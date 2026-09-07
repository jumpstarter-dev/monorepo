package jumpstarter

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	operatorv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/deploy/operator/api/v1alpha1"
)

// reconcileRBAC reconciles all RBAC resources (ServiceAccount, Role, RoleBinding)
func (r *JumpstarterReconciler) reconcileRBAC(ctx context.Context, jumpstarter *operatorv1alpha1.Jumpstarter) error {
	log := logf.FromContext(ctx)

	// ServiceAccount
	// Note: We intentionally do NOT set controller reference on ServiceAccount to prevent
	// it from being garbage collected when the Jumpstarter CR is deleted
	desiredSA := r.createServiceAccount(jumpstarter)

	existingSA := &corev1.ServiceAccount{}
	existingSA.Name = desiredSA.Name
	existingSA.Namespace = desiredSA.Namespace

	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, existingSA, func() error {
		// Check if this is a new service account or an existing one
		if existingSA.CreationTimestamp.IsZero() {
			// ServiceAccount is being created, copy all fields from desired
			existingSA.Labels = desiredSA.Labels
			existingSA.Annotations = desiredSA.Annotations
			return nil
		}

		// ServiceAccount exists, check if update is needed
		if !serviceAccountNeedsUpdate(existingSA, desiredSA) {
			log.V(1).Info("ServiceAccount is up to date, skipping update",
				"name", existingSA.Name,
				"namespace", existingSA.Namespace)
			return nil
		}

		// Update needed - apply changes
		existingSA.Labels = desiredSA.Labels
		existingSA.Annotations = desiredSA.Annotations
		return nil
	})

	if err != nil {
		log.Error(err, "Failed to reconcile ServiceAccount",
			"name", desiredSA.Name,
			"namespace", desiredSA.Namespace)
		return err
	}

	log.Info("ServiceAccount reconciled",
		"name", existingSA.Name,
		"namespace", existingSA.Namespace,
		"operation", op)

	// Role
	desiredRole := r.createRole(jumpstarter)

	existingRole := &rbacv1.Role{}
	existingRole.Name = desiredRole.Name
	existingRole.Namespace = desiredRole.Namespace

	op, err = controllerutil.CreateOrUpdate(ctx, r.Client, existingRole, func() error {
		// Check if this is a new role or an existing one
		if existingRole.CreationTimestamp.IsZero() {
			// Role is being created, copy all fields from desired
			existingRole.Labels = desiredRole.Labels
			existingRole.Annotations = desiredRole.Annotations
			existingRole.Rules = desiredRole.Rules
			return controllerutil.SetControllerReference(jumpstarter, existingRole, r.Scheme)
		}

		// Role exists, check if update is needed
		if !roleNeedsUpdate(existingRole, desiredRole) {
			log.V(1).Info("Role is up to date, skipping update",
				"name", existingRole.Name,
				"namespace", existingRole.Namespace)
			return nil
		}

		// Update needed - apply changes
		existingRole.Labels = desiredRole.Labels
		existingRole.Annotations = desiredRole.Annotations
		existingRole.Rules = desiredRole.Rules
		return controllerutil.SetControllerReference(jumpstarter, existingRole, r.Scheme)
	})

	if err != nil {
		log.Error(err, "Failed to reconcile Role",
			"name", desiredRole.Name,
			"namespace", desiredRole.Namespace)
		return err
	}

	log.Info("Role reconciled",
		"name", existingRole.Name,
		"namespace", existingRole.Namespace,
		"operation", op)

	// RoleBinding
	desiredRoleBinding := r.createRoleBinding(jumpstarter)

	existingRoleBinding := &rbacv1.RoleBinding{}
	existingRoleBinding.Name = desiredRoleBinding.Name
	existingRoleBinding.Namespace = desiredRoleBinding.Namespace

	op, err = controllerutil.CreateOrUpdate(ctx, r.Client, existingRoleBinding, func() error {
		// Check if this is a new role binding or an existing one
		if existingRoleBinding.CreationTimestamp.IsZero() {
			// RoleBinding is being created, copy all fields from desired
			existingRoleBinding.Labels = desiredRoleBinding.Labels
			existingRoleBinding.Annotations = desiredRoleBinding.Annotations
			existingRoleBinding.Subjects = desiredRoleBinding.Subjects
			existingRoleBinding.RoleRef = desiredRoleBinding.RoleRef
			return controllerutil.SetControllerReference(jumpstarter, existingRoleBinding, r.Scheme)
		}

		// RoleBinding exists, check if update is needed
		if !roleBindingNeedsUpdate(existingRoleBinding, desiredRoleBinding) {
			log.V(1).Info("RoleBinding is up to date, skipping update",
				"name", existingRoleBinding.Name,
				"namespace", existingRoleBinding.Namespace)
			return nil
		}

		// Update needed - apply changes
		existingRoleBinding.Labels = desiredRoleBinding.Labels
		existingRoleBinding.Annotations = desiredRoleBinding.Annotations
		existingRoleBinding.Subjects = desiredRoleBinding.Subjects
		existingRoleBinding.RoleRef = desiredRoleBinding.RoleRef
		return controllerutil.SetControllerReference(jumpstarter, existingRoleBinding, r.Scheme)
	})

	if err != nil {
		log.Error(err, "Failed to reconcile RoleBinding",
			"name", desiredRoleBinding.Name,
			"namespace", desiredRoleBinding.Namespace)
		return err
	}

	log.Info("RoleBinding reconciled",
		"name", existingRoleBinding.Name,
		"namespace", existingRoleBinding.Namespace,
		"operation", op)

	return nil
}

// createServiceAccount creates a service account for the controller
func (r *JumpstarterReconciler) createServiceAccount(jumpstarter *operatorv1alpha1.Jumpstarter) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-controller-manager", jumpstarter.Name),
			Namespace: jumpstarter.Namespace,
			Labels: map[string]string{
				"app":                          "jumpstarter-controller",
				"app.kubernetes.io/name":       "jumpstarter-controller",
				"app.kubernetes.io/managed-by": "jumpstarter-operator",
			},
		},
	}
}

// createRole creates a role with necessary permissions for the controller
func (r *JumpstarterReconciler) createRole(jumpstarter *operatorv1alpha1.Jumpstarter) *rbacv1.Role {
	return &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-controller-role", jumpstarter.Name),
			Namespace: jumpstarter.Namespace,
			Labels: map[string]string{
				"app":                          "jumpstarter-controller",
				"app.kubernetes.io/name":       "jumpstarter-controller",
				"app.kubernetes.io/managed-by": "jumpstarter-operator",
			},
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{""},
				Resources: []string{"configmaps"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{""},
				Resources: []string{"secrets"},
				Verbs:     []string{"get", "list", "watch", "create", "update", "patch", "delete"},
			},
			{
				APIGroups: []string{"jumpstarter.dev"},
				Resources: []string{"clients", "exporters", "leases", "exporteraccesspolicies"},
				Verbs:     []string{"get", "list", "watch", "create", "update", "patch", "delete"},
			},
			{
				APIGroups: []string{"jumpstarter.dev"},
				Resources: []string{"clients/status", "exporters/status", "leases/status", "exporteraccesspolicies/status"},
				Verbs:     []string{"get", "update", "patch"},
			},
			{
				APIGroups: []string{"jumpstarter.dev"},
				Resources: []string{"clients/finalizers", "exporters/finalizers", "leases/finalizers", "exporteraccesspolicies/finalizers"},
				Verbs:     []string{"update"},
			},
			{
				// Read-only: the lease controller asks whether a selector that
				// matches no exporter today could be satisfied by a pool
				// provisioning one, which decides between pending and
				// unsatisfiable. It never writes exporter sets.
				APIGroups: []string{"virtualtarget.jumpstarter.dev"},
				Resources: []string{"exportersets"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{""},
				Resources: []string{"events"},
				Verbs:     []string{"create", "patch"},
			},
			{
				APIGroups: []string{"coordination.k8s.io"},
				Resources: []string{"leases"},
				Verbs:     []string{"get", "list", "watch", "create", "update", "patch", "delete"},
			},
		},
	}
}

// createRoleBinding creates a role binding for the controller
func (r *JumpstarterReconciler) createRoleBinding(jumpstarter *operatorv1alpha1.Jumpstarter) *rbacv1.RoleBinding {
	return &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-controller-rolebinding", jumpstarter.Name),
			Namespace: jumpstarter.Namespace,
			Labels: map[string]string{
				"app":                          "jumpstarter-controller",
				"app.kubernetes.io/name":       "jumpstarter-controller",
				"app.kubernetes.io/managed-by": "jumpstarter-operator",
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "Role",
			Name:     fmt.Sprintf("%s-controller-role", jumpstarter.Name),
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      fmt.Sprintf("%s-controller-manager", jumpstarter.Name),
				Namespace: jumpstarter.Namespace,
			},
		},
	}
}
