/*
Copyright 2024.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"time"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	jmpmetrics "github.com/jumpstarter-dev/jumpstarter/controller/internal/metrics"
	corev1 "k8s.io/api/core/v1"
	k8serrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// LeaseReconciler reconciles a Lease object
type LeaseReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// ApprovedExporter represents an exporter that has been approved for leasing,
// along with its associated policy and any existing lease.
type ApprovedExporter struct {
	// Exporter is the approved exporter
	Exporter jumpstarterdevv1alpha1.Exporter
	// ExistingLease is a pointer to any existing lease for this exporter, or nil if none exists
	ExistingLease *jumpstarterdevv1alpha1.Lease
	// Policy represents the access policy that approved this exporter
	Policy jumpstarterdevv1alpha1.Policy
}

// +kubebuilder:rbac:groups=jumpstarter.dev,resources=leases,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=leases/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=jumpstarter.dev,resources=leases/finalizers,verbs=update
// +kubebuilder:rbac:groups=virtualtarget.jumpstarter.dev,resources=exportersets,verbs=get;list;watch
// Reconcile is part of the main kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state.
// TODO(user): Modify the Reconcile function to compare the state specified by
// the Lease object against the actual cluster state, and then
// perform operations to make the cluster state reflect the state specified by
// the user.
//
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.18.4/pkg/reconcile
func (r *LeaseReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	ctx = ctrl.LoggerInto(ctx, logger)

	var lease jumpstarterdevv1alpha1.Lease
	if err := r.Get(ctx, req.NamespacedName, &lease); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(
			fmt.Errorf("Reconcile: unable to get lease: %w", err),
		)
	}

	leaseLogValues := []interface{}{"lease_id", lease.Name, "client", lease.Spec.ClientRef.Name}
	if lease.Spec.ExporterRef != nil {
		leaseLogValues = append(leaseLogValues, "exporter", lease.Spec.ExporterRef.Name)
	}
	for k, v := range lease.Spec.Context {
		leaseLogValues = append(leaseLogValues, k, v)
	}
	logger = logger.WithValues(leaseLogValues...)
	ctx = ctrl.LoggerInto(ctx, logger)

	var result ctrl.Result
	priorExporterRef := lease.Status.ExporterRef
	priorUnsatisfiable := meta.IsStatusConditionTrue(
		lease.Status.Conditions,
		string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
	)

	if err := r.reconcileStatusExporterRef(ctx, &result, &lease); err != nil {
		return result, err
	}

	if err := r.reconcileStatusBeginEndTimes(ctx, &lease); err != nil {
		return result, err
	}

	if err := r.reconcileStatusEnded(ctx, &result, &lease); err != nil {
		return result, err
	}

	if err := r.Status().Update(ctx, &lease); err != nil {
		return RequeueConflict(logger, result, err)
	}

	// Record acquisition only after status is persisted so a failed Update
	// cannot double-count on requeue (success or failure).
	recordLeaseAcquisitionTransition(ctx, &lease, priorExporterRef, priorUnsatisfiable)

	if lease.Labels == nil {
		lease.Labels = make(map[string]string)
	}
	if lease.Status.Ended {
		lease.Labels[string(jumpstarterdevv1alpha1.LeaseLabelEnded)] = jumpstarterdevv1alpha1.LeaseLabelEndedValue
	}

	if lease.Status.ExporterRef != nil {
		var exporter jumpstarterdevv1alpha1.Exporter
		if err := r.Get(ctx, types.NamespacedName{
			Namespace: lease.Namespace,
			Name:      lease.Status.ExporterRef.Name,
		}, &exporter); err != nil {
			return result, err
		}
		if err := controllerutil.SetControllerReference(&exporter, &lease, r.Scheme); err != nil {
			return result, fmt.Errorf("Reconcile: failed to update lease controller reference: %w", err)
		}
	}

	if err := r.Update(ctx, &lease); err != nil {
		return RequeueConflict(logger, result, fmt.Errorf("Reconcile: failed to update lease metadata: %w", err))
	}

	return result, nil
}

// also manages EndTime and LeaseConditionTypeReady
// nolint:unparam
func (r *LeaseReconciler) reconcileStatusEnded(
	ctx context.Context,
	result *ctrl.Result,
	lease *jumpstarterdevv1alpha1.Lease,
) error {

	now := time.Now()
	if !lease.Status.Ended {
		// if lease has status condition unsatisfiable or invalid, we mark it as ended to avoid reprocessing
		if meta.IsStatusConditionTrue(lease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable)) ||
			meta.IsStatusConditionTrue(lease.Status.Conditions, string(jumpstarterdevv1alpha1.LeaseConditionTypeInvalid)) {
			lease.Status.Ended = true
			lease.Status.EndTime = &metav1.Time{Time: now}
			return nil
		} else if lease.Spec.Release {
			lease.Release(ctx)
			return nil
		} else if lease.Status.BeginTime != nil {
			var expiration time.Time
			if lease.Spec.EndTime != nil {
				// expires at Spec.EndTime when specified
				expiration = lease.Spec.EndTime.Time
			} else if lease.Spec.BeginTime != nil && lease.Spec.Duration != nil {
				// expires at Spec.BeginTime + Spec.Duration - scheduled lease
				expiration = lease.Spec.BeginTime.Add(lease.Spec.Duration.Duration)
			} else if lease.Spec.Duration != nil {
				// expires at actual BeginTime + Spec.Duration - immediate lease
				expiration = lease.Status.BeginTime.Add(lease.Spec.Duration.Duration)
			}

			if expiration.Before(now) {
				lease.Expire(ctx)
				return nil
			}
			result.RequeueAfter = expiration.Sub(now)
			return nil
		}

	}
	return nil
}

// nolint:unparam
func (r *LeaseReconciler) reconcileStatusBeginEndTimes(
	ctx context.Context,
	lease *jumpstarterdevv1alpha1.Lease,
) error {
	if lease.Status.BeginTime == nil && lease.Status.ExporterRef != nil {
		logger := log.FromContext(ctx)
		logger.Info("Updating begin time for lease", "lease", lease.Name, "exporter", lease.GetExporterName(), "client", lease.GetClientName())
		now := time.Now()
		lease.Status.BeginTime = &metav1.Time{Time: now}
		lease.SetStatusReady(true, "Ready", "An exporter has been acquired for the client")
	}

	return nil
}

// candidateExporters resolves the exporters a lease could be given: the one it
// names, or every exporter its selector matches.
//
// It reports decided=true when it has already settled the lease's status and
// the caller should stop — the named exporter is missing, does not match, or is
// disabled; every match is disabled; or nothing matches yet and a pool can
// provide one.
func (r *LeaseReconciler) candidateExporters(
	ctx context.Context,
	result *ctrl.Result,
	lease *jumpstarterdevv1alpha1.Lease,
	selector labels.Selector,
) ([]jumpstarterdevv1alpha1.Exporter, bool, error) {
	if lease.Spec.ExporterRef != nil {
		var exporter jumpstarterdevv1alpha1.Exporter
		if err := r.Get(ctx, types.NamespacedName{
			Namespace: lease.Namespace,
			Name:      lease.Spec.ExporterRef.Name,
		}, &exporter); err != nil {
			if k8serrors.IsNotFound(err) {
				lease.SetStatusUnsatisfiable(
					"ExporterNotFound",
					"Requested exporter %s was not found",
					lease.Spec.ExporterRef.Name,
				)
				return nil, true, nil
			}
			return nil, false, fmt.Errorf("candidateExporters: failed to get requested exporter: %w", err)
		}
		if !selector.Empty() && !selector.Matches(labels.Set(exporter.Labels)) {
			lease.SetStatusUnsatisfiable(
				"SelectorMismatch",
				"Requested exporter %s does not match selector %s",
				exporter.Name,
				metav1.FormatLabelSelector(&lease.Spec.Selector),
			)
			return nil, true, nil
		}
		// Check if the explicitly requested exporter is disabled
		if !exporter.IsEnabled() && !lease.Spec.AllowDisabled {
			lease.SetStatusUnsatisfiable(
				"ExporterDisabled",
				"Requested exporter %s is disabled. "+
					"To lease a disabled exporter, set spec.allowDisabled: true on the Lease, "+
					"or use --allow-disabled with jmp create lease or jmp shell",
				exporter.Name,
			)
			return nil, true, nil
		}
		return []jumpstarterdevv1alpha1.Exporter{exporter}, false, nil
	}

	// List all exporters matching selector
	listed, err := r.ListMatchingExporters(ctx, lease, selector)
	if err != nil {
		return nil, false, fmt.Errorf("candidateExporters: failed to list matching exporters: %w", err)
	}
	if len(listed.Items) == 0 {
		// Nothing matches yet, which is the normal state of a pool that
		// keeps nothing warm. An exporter set that could provision a
		// match makes this lease pending rather than unsatisfiable —
		// and pending is what the set counts as demand to scale up on.
		// Calling it unsatisfiable here ends the lease in this same
		// reconcile, so the set would never see it.
		set, err := r.exporterSetCanProvision(ctx, lease, selector)
		if err != nil {
			return nil, false, fmt.Errorf("candidateExporters: %w", err)
		}
		if set != "" {
			lease.SetStatusPending(
				"Provisioning",
				"No exporter matches the selector yet; exporter set %s can provision one",
				set,
			)
			result.RequeueAfter = pendingRequeueAfter(lease)
			return nil, true, nil
		}
	}

	// Filter out disabled exporters from selector-based listing
	matchingExporters := filterOutDisabledExporters(listed.Items)
	if len(matchingExporters) == 0 && len(listed.Items) > 0 {
		lease.SetStatusUnsatisfiable(
			"AllDisabled",
			"All %d exporters matching the selector are disabled",
			len(listed.Items),
		)
		return nil, true, nil
	}
	return matchingExporters, false, nil
}

// Also manages LeaseConditionTypeUnsatisfiable and LeaseConditionTypePending
func (r *LeaseReconciler) reconcileStatusExporterRef(
	ctx context.Context,
	result *ctrl.Result,
	lease *jumpstarterdevv1alpha1.Lease,
) error {
	logger := log.FromContext(ctx)

	// Do not attempt to reconcile if the lease is already ended/invalid/etc
	if lease.Status.Ended {
		return nil
	}

	if lease.Status.ExporterRef == nil {
		// For scheduled leases: only assign exporter if requested BeginTime has arrived
		if lease.Spec.BeginTime != nil {
			now := time.Now()
			if lease.Spec.BeginTime.After(now) {
				// Requested BeginTime is in the future, wait until then
				waitDuration := lease.Spec.BeginTime.Sub(now)
				logger.Info("Lease is scheduled for the future, waiting",
					"lease", lease.Name,
					"requestedBeginTime", lease.Spec.BeginTime,
					"waitDuration", waitDuration)
				result.RequeueAfter = waitDuration
				return nil
			}
		}
		logger.Info("Looking for a matching exporter for lease", "lease", lease.Name, "client", lease.GetClientName(), "selector", lease.Spec.Selector)

		selector, err := lease.GetExporterSelector()
		if err != nil {
			return fmt.Errorf("reconcileStatusExporterRef: failed to get exporter selector: %w", err)
		} else if selector.Empty() && lease.Spec.ExporterRef == nil {
			lease.SetStatusInvalid("InvalidSelector", "The selector for the lease is empty, a selector is required")
			return nil
		}

		matchingExporters, decided, err := r.candidateExporters(ctx, result, lease, selector)
		if err != nil {
			return err
		}
		if decided {
			return nil
		}

		approvedExporters, unmatchedDescriptions, err := r.attachMatchingPolicies(ctx, lease, matchingExporters)
		if err != nil {
			return fmt.Errorf("reconcileStatusExporterRef: failed to handle policy approval: %w", err)
		}

		if len(approvedExporters) == 0 {
			if len(unmatchedDescriptions) > 0 {
				desc := strings.Join(unmatchedDescriptions, "; ")
				if len(desc) > 4096 {
					desc = desc[:4096] + "..."
				}
				lease.SetStatusUnsatisfiable("NoAccess",
					"While there are %d exporters matching the selector, none of them are approved by any policy for your client. Matching policies: %s",
					len(matchingExporters), desc,
				)
			} else {
				lease.SetStatusUnsatisfiable("NoAccess",
					"While there are %d exporters matching the selector, none of them are approved by any policy for your client",
					len(matchingExporters),
				)
			}
			return nil
		}

		onlineApprovedExporters := filterOutOfflineExporters(approvedExporters)
		if len(onlineApprovedExporters) == 0 {
			lease.SetStatusPending(
				"Offline",
				"While there are %d available exporters (i.e. %s), none of them are online",
				len(approvedExporters),
				approvedExporters[0].Exporter.Name,
			)
			result.RequeueAfter = pendingRequeueAfter(lease)
			return nil
		}

		// Filter out exporters that are already leased
		activeLeases, err := r.ListActiveLeases(ctx, lease.Namespace)
		if err != nil {
			return fmt.Errorf("reconcileStatusExporterRef: failed to list active leases: %w", err)
		}

		onlineApprovedExporters = attachExistingLeases(onlineApprovedExporters, activeLeases.Items)
		orderedExporters := orderApprovedExporters(onlineApprovedExporters)

		if len(orderedExporters) > 0 && orderedExporters[0].Policy.SpotAccess {
			lease.SetStatusUnsatisfiable("SpotAccess",
				"The only possible exporters are under spot access (i.e. %s), but spot access is still not implemented",
				orderedExporters[0].Exporter.Name)
			return nil
		}

		availableExporters := filterOutLeasedExporters(onlineApprovedExporters)
		if len(availableExporters) == 0 {
			lease.SetStatusPending("NotAvailable",
				"There are %d approved exporters, (i.e. %s) but all of them are already leased",
				len(onlineApprovedExporters),
				onlineApprovedExporters[0].Exporter.Name,
			)
			result.RequeueAfter = pendingRequeueAfter(lease)
			return nil
		}

		readyAvailableExporters := filterOutNotReadyExporters(availableExporters)
		if len(readyAvailableExporters) == 0 {
			lease.SetStatusPending(
				"NotReady",
				"There are %d online exporters, but none are ready (still cleaning up previous lease)",
				len(availableExporters),
			)
			result.RequeueAfter = pendingRequeueAfter(lease)
			return nil
		}

		// TODO: here there's room for improvement, i.e. we could have multiple
		// clients trying to lease the same exporters, we should look at priorities
		// and spot access to decide which client gets the exporter, this probably means
		// that we will need to construct a lease scheduler with the view of all leases
		// and exporters in the system, and (maybe) a priority queue for the leases.

		// For now, we just select the best available exporter without considering other
		// ongoing lease requests

		selected := readyAvailableExporters[0]

		if selected.ExistingLease != nil {
			// TODO: Implement eviction of spot access leases
			lease.SetStatusPending("NotAvailable",
				"Exporter %s is already leased by another client under spot access, but spot access eviction still not implemented",
				selected.Exporter.Name)
			result.RequeueAfter = pendingRequeueAfter(lease)
			return nil
		}

		lease.Status.Priority = selected.Policy.Priority
		lease.Status.SpotAccess = selected.Policy.SpotAccess
		lease.Status.ExporterRef = &corev1.LocalObjectReference{
			Name: selected.Exporter.Name,
		}
		return nil
	}

	return nil
}

// leaseAcquisitionTransitionResult returns the metric result for a persisted
// status transition, or ("", false) when no acquisition should be recorded.
// Recording is based on the pre-reconcile persisted snapshot so requeues after
// a successful Status().Update do not double-count.
func leaseAcquisitionTransitionResult(
	priorExporterRef *corev1.LocalObjectReference,
	priorUnsatisfiable bool,
	lease *jumpstarterdevv1alpha1.Lease,
) (string, bool) {
	if lease == nil {
		return "", false
	}
	if priorExporterRef == nil && lease.Status.ExporterRef != nil {
		return jmpmetrics.ResultSuccess, true
	}
	nowUnsatisfiable := meta.IsStatusConditionTrue(
		lease.Status.Conditions,
		string(jumpstarterdevv1alpha1.LeaseConditionTypeUnsatisfiable),
	)
	if !priorUnsatisfiable && nowUnsatisfiable {
		return jmpmetrics.ResultFailure, true
	}
	return "", false
}

func recordLeaseAcquisitionTransition(
	ctx context.Context,
	lease *jumpstarterdevv1alpha1.Lease,
	priorExporterRef *corev1.LocalObjectReference,
	priorUnsatisfiable bool,
) {
	result, ok := leaseAcquisitionTransitionResult(priorExporterRef, priorUnsatisfiable, lease)
	if !ok {
		return
	}
	recordLeaseAcquisition(ctx, lease, result)
}

func recordLeaseAcquisition(ctx context.Context, lease *jumpstarterdevv1alpha1.Lease, result string) {
	exemplars := map[string]string{}
	if lease != nil {
		exemplars["lease_id"] = lease.Name
		if lease.Spec.ClientRef.Name != "" {
			exemplars["client"] = lease.Spec.ClientRef.Name
		}
	}
	jmpmetrics.Default.RecordAcquisition(ctx, result, exemplars)
}

// attachMatchingPolicies attaches the matching policies to the list of online exporters
// if the exporter matches the policy and the client matches the policy's client selector
// the exporter is approved for leasing
func (r *LeaseReconciler) attachMatchingPolicies(ctx context.Context, lease *jumpstarterdevv1alpha1.Lease, onlineExporters []jumpstarterdevv1alpha1.Exporter) ([]ApprovedExporter, []string, error) {
	var approvedExporters []ApprovedExporter

	var policies jumpstarterdevv1alpha1.ExporterAccessPolicyList
	if err := r.List(ctx, &policies,
		client.InNamespace(lease.Namespace),
	); err != nil {
		return nil, nil, fmt.Errorf("reconcileStatusExporterRef: failed to list exporter access policies: %w", err)
	}

	// If there are no policies, we just approve all online exporters
	if len(policies.Items) == 0 {
		for _, exporter := range onlineExporters {
			approvedExporters = append(approvedExporters, ApprovedExporter{
				Exporter: exporter,
				Policy: jumpstarterdevv1alpha1.Policy{
					Priority:   0,
					SpotAccess: false,
				},
			})
		}
		return approvedExporters, nil, nil
	}
	// If policies exist: get the client to obtain the metadata necessary for policy matching
	var jclient jumpstarterdevv1alpha1.Client
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: lease.Namespace,
		Name:      lease.Spec.ClientRef.Name,
	}, &jclient); err != nil {
		return nil, nil, fmt.Errorf("reconcileStatusExporterRef: failed to get client: %w", err)
	}

	seenDescriptions := make(map[string]bool)
	var unmatchedDescriptions []string

	for _, exporter := range onlineExporters {
		for _, policy := range policies.Items {
			exporterSelector, err := metav1.LabelSelectorAsSelector(&policy.Spec.ExporterSelector)
			if err != nil {
				return nil, nil, fmt.Errorf("reconcileStatusExporterRef: failed to convert exporter selector: %w", err)
			}
			if exporterSelector.Matches(labels.Set(exporter.Labels)) {
				for _, p := range policy.Spec.Policies {
					clientMatched := false
					for _, from := range p.From {
						clientSelector, err := metav1.LabelSelectorAsSelector(&from.ClientSelector)
						if err != nil {
							return nil, nil, fmt.Errorf("reconcileStatusExporterRef: failed to convert client selector: %w", err)
						}
						if clientSelector.Matches(labels.Set(jclient.Labels)) {
							clientMatched = true
							if p.MaximumDuration != nil {
								// Calculate requested duration (may be from explicit Duration or computed from times)
								requestedDuration := time.Duration(0)
								if lease.Spec.Duration != nil {
									requestedDuration = lease.Spec.Duration.Duration
								} else if lease.Spec.BeginTime != nil && lease.Spec.EndTime != nil {
									requestedDuration = lease.Spec.EndTime.Sub(lease.Spec.BeginTime.Time)
								}
								if requestedDuration > p.MaximumDuration.Duration {
									// TODO: we probably should keep this on the list of approved exporters
									// but mark as excessive duration so we can report it on the status
									// of lease if no other options exist
									continue
								}
							}
							approvedExporters = append(approvedExporters, ApprovedExporter{
								Exporter: exporter,
								Policy:   p,
							})
						}
					}
					if !clientMatched && p.Description != "" && !seenDescriptions[p.Description] {
						seenDescriptions[p.Description] = true
						unmatchedDescriptions = append(unmatchedDescriptions, p.Description)
					}
				}
			}
		}
	}
	return approvedExporters, unmatchedDescriptions, nil
}

// ListMatchingExporters returns a list of exporters that match the selector of the lease
func (r *LeaseReconciler) ListMatchingExporters(ctx context.Context, lease *jumpstarterdevv1alpha1.Lease,
	selector labels.Selector) (*jumpstarterdevv1alpha1.ExporterList, error) {

	var matchingExporters jumpstarterdevv1alpha1.ExporterList
	if err := r.List(
		ctx,
		&matchingExporters,
		client.InNamespace(lease.Namespace),
		client.MatchingLabelsSelector{Selector: selector},
	); err != nil {
		return nil, fmt.Errorf("ListMatchingExporters: failed to list exporters matching selector: %w", err)
	}
	return &matchingExporters, nil
}

// exporterSetCanProvision reports the name of an exporter set that could
// provision an exporter matching the lease's selector, or "" if none can.
//
// It answers the question the lease controller cannot otherwise ask: is there
// nothing for this lease because nothing will ever match, or because the pool
// that would match keeps nothing warm? The two look identical from the exporter
// listing and mean opposite things to the caller.
//
// A set qualifies when the labels it stamps on its members satisfy the selector
// and it is not already at its ceiling. Whether it is actually willing to scale
// right now is the set's own decision, made in its reconciler; this only has to
// be right about whether waiting is worthwhile.
func (r *LeaseReconciler) exporterSetCanProvision(
	ctx context.Context,
	lease *jumpstarterdevv1alpha1.Lease,
	selector labels.Selector,
) (string, error) {
	var sets virtualtargetv1alpha1.ExporterSetList
	if err := r.List(ctx, &sets, client.InNamespace(lease.Namespace)); err != nil {
		if k8serrors.IsForbidden(err) || meta.IsNoMatchError(err) {
			// A controller running against an operator that has not granted
			// this read, or a cluster without the exporter set CRDs at all.
			// Neither is transient, and neither is a reason to fail a lease:
			// fall back to the behaviour from before pools existed.
			log.FromContext(ctx).V(1).Info(
				"cannot read exporter sets, treating the selector as unsatisfiable",
				"lease", lease.Name, "reason", err.Error())
			return "", nil
		}
		return "", fmt.Errorf("exporterSetCanProvision: failed to list exporter sets: %w", err)
	}

	for i := range sets.Items {
		set := &sets.Items[i]
		if !selector.Matches(labels.Set(set.Spec.Template.Metadata.Labels)) {
			continue
		}
		if set.Spec.MaxReplicas > 0 && set.Status.Replicas >= set.Spec.MaxReplicas {
			continue
		}
		return set.Name, nil
	}
	return "", nil
}

// ListActiveLeases returns a list of active leases in the namespace
func (r *LeaseReconciler) ListActiveLeases(ctx context.Context, namespace string) (*jumpstarterdevv1alpha1.LeaseList, error) {
	var activeLeases jumpstarterdevv1alpha1.LeaseList
	if err := r.List(
		ctx,
		&activeLeases,
		client.InNamespace(namespace),
		MatchingActiveLeases(),
	); err != nil {
		return nil, err
	}
	return &activeLeases, nil
}

// attachExistingLeases attaches the existing leases to the approved exporter list
// if the activeLeases slice contains a lease that references the exporter in the
// approved exporter list
func attachExistingLeases(exporters []ApprovedExporter, activeLeases []jumpstarterdevv1alpha1.Lease) []ApprovedExporter {
	for i, exporter := range exporters {
		for _, existingLease := range activeLeases {
			if existingLease.Status.ExporterRef != nil &&
				existingLease.Status.ExporterRef.Name == exporter.Exporter.Name {
				exporters[i].ExistingLease = &existingLease
			}
		}
	}
	return exporters
}

// orderAvailableExporters orders the exporters in the following order
// 1. Not being leased
// 2. Not accessible under spot access
// 3. Highest priority
// 4. Alphabetically by exporter name

func orderApprovedExporters(exporters []ApprovedExporter) []ApprovedExporter {
	// Order by lease status, priority, spot access, and name

	cmpFunc := func(a, b ApprovedExporter) int {
		// If one of the exporters has an existing lease, we want to prioritize the one that doesn't
		if a.ExistingLease != nil && b.ExistingLease == nil {
			return 1
		} else if a.ExistingLease == nil && b.ExistingLease != nil {
			return -1
		}

		// We want spot access policies to be later on the returned array
		if a.Policy.SpotAccess != b.Policy.SpotAccess {
			if a.Policy.SpotAccess {
				return 1
			}
			return -1
		}

		// We want the highest priority to be first
		if a.Policy.Priority != b.Policy.Priority {
			return b.Policy.Priority - a.Policy.Priority
		}

		// If the priority is the same, we want to sort by exporter name
		return strings.Compare(a.Exporter.Name, b.Exporter.Name)
	}

	slices.SortFunc(exporters, cmpFunc)

	return exporters
}

// filterOutLeasedExporters filters out the exporters that are already leased
func filterOutLeasedExporters(exporters []ApprovedExporter) []ApprovedExporter {
	// Exclude exporter that are already leased and non-takeable
	return slices.DeleteFunc(exporters, func(ae ApprovedExporter) bool {
		existingLease := ae.ExistingLease
		if existingLease == nil {
			return false
		}

		weHaveNonSpotAccess := !ae.Policy.SpotAccess

		// There is an existing lease, but, if it's spot access we can take it
		if weHaveNonSpotAccess && ae.ExistingLease.Status.SpotAccess {
			return false
		}

		// ok, there is an existing lease, and it's not spot access, we can't take it
		return true
	})

}

// filterOutNotReadyExporters filters out exporters that are not in a ready state
// to accept new leases. Only exporters with Available status (or unset status for
// backwards compatibility with old exporters) are considered ready.
func filterOutNotReadyExporters(approvedExporters []ApprovedExporter) []ApprovedExporter {
	return slices.DeleteFunc(
		slices.Clone(approvedExporters),
		func(approvedExporter ApprovedExporter) bool {
			status := approvedExporter.Exporter.Status.ExporterStatusValue
			// Allow Available or unset (backwards compat with old exporters that don't report status)
			return status != jumpstarterdevv1alpha1.ExporterStatusAvailable &&
				status != jumpstarterdevv1alpha1.ExporterStatusUnspecified &&
				status != ""
		},
	)
}

// filterOutDisabledExporters removes exporters that have spec.enabled set to false.
// Exporters with nil Enabled (backward compatibility) or Enabled=true are kept.
func filterOutDisabledExporters(exporters []jumpstarterdevv1alpha1.Exporter) []jumpstarterdevv1alpha1.Exporter {
	return slices.DeleteFunc(
		slices.Clone(exporters),
		func(exporter jumpstarterdevv1alpha1.Exporter) bool {
			return !exporter.IsEnabled()
		},
	)
}

// filterOutOfflineExporters filters out the exporters that are not online
func filterOutOfflineExporters(approvedExporters []ApprovedExporter) []ApprovedExporter {
	onlineExporters := slices.DeleteFunc(
		approvedExporters,
		func(approvedExporter ApprovedExporter) bool {
			return !meta.IsStatusConditionTrue(
				approvedExporter.Exporter.Status.Conditions,
				string(jumpstarterdevv1alpha1.ExporterConditionTypeRegistered),
			) || !meta.IsStatusConditionTrue(
				approvedExporter.Exporter.Status.Conditions,
				string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
			)
		},
	)
	return onlineExporters
}

// isLeaseEnded checks whether a lease object carries the ended label.
func isLeaseEnded(obj client.Object) bool {
	if v, ok := obj.GetLabels()[string(jumpstarterdevv1alpha1.LeaseLabelEnded)]; ok {
		return v == jumpstarterdevv1alpha1.LeaseLabelEndedValue
	}
	return false
}

// skipEndedPredicate returns a predicate that filters out leases carrying the
// ended label. Leases without the label are admitted so the reconciler can
// backfill it when Status.Ended is true but the label write was lost.
func skipEndedPredicate() predicate.Funcs {
	return predicate.Funcs{
		CreateFunc: func(e event.CreateEvent) bool {
			return !isLeaseEnded(e.Object)
		},
		UpdateFunc: func(e event.UpdateEvent) bool {
			return !isLeaseEnded(e.ObjectNew)
		},
		DeleteFunc: func(e event.DeleteEvent) bool {
			return true
		},
		GenericFunc: func(e event.GenericEvent) bool {
			return !isLeaseEnded(e.Object)
		},
	}
}

const maxPendingRequeue = 30 * time.Second

func pendingRequeueAfter(lease *jumpstarterdevv1alpha1.Lease) time.Duration {
	cond := meta.FindStatusCondition(
		lease.Status.Conditions,
		string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
	)
	if cond == nil {
		return time.Second
	}
	elapsed := time.Since(cond.LastTransitionTime.Time)
	backoff := time.Second
	for backoff < maxPendingRequeue && backoff < elapsed {
		backoff *= 2
	}
	if backoff > maxPendingRequeue {
		backoff = maxPendingRequeue
	}
	return backoff
}

func exporterChangedForPendingLeases() predicate.Funcs {
	meaningful := func(oldExp, newExp *jumpstarterdevv1alpha1.Exporter) bool {
		if oldExp.Status.ExporterStatusValue != newExp.Status.ExporterStatusValue {
			return true
		}
		oldOnline := meta.IsStatusConditionTrue(oldExp.Status.Conditions,
			string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline))
		newOnline := meta.IsStatusConditionTrue(newExp.Status.Conditions,
			string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline))
		if oldOnline != newOnline {
			return true
		}
		oldLeased := oldExp.Status.LeaseRef != nil
		newLeased := newExp.Status.LeaseRef != nil
		if oldLeased != newLeased {
			return true
		}
		if !labels.Equals(labels.Set(oldExp.Labels), labels.Set(newExp.Labels)) {
			return true
		}
		return false
	}
	return predicate.Funcs{
		CreateFunc: func(e event.CreateEvent) bool {
			return true
		},
		UpdateFunc: func(e event.UpdateEvent) bool {
			oldExp := e.ObjectOld.(*jumpstarterdevv1alpha1.Exporter)
			newExp := e.ObjectNew.(*jumpstarterdevv1alpha1.Exporter)
			return meaningful(oldExp, newExp)
		},
		DeleteFunc: func(e event.DeleteEvent) bool {
			return false
		},
		GenericFunc: func(e event.GenericEvent) bool {
			return true
		},
	}
}

func (r *LeaseReconciler) mapExporterToLeases(ctx context.Context, obj client.Object) []reconcile.Request {
	exporter := obj.(*jumpstarterdevv1alpha1.Exporter)

	var leaseList jumpstarterdevv1alpha1.LeaseList
	if err := r.List(ctx, &leaseList, client.InNamespace(exporter.Namespace), MatchingActiveLeases()); err != nil {
		return nil
	}

	var requests []reconcile.Request
	for i := range leaseList.Items {
		lease := &leaseList.Items[i]
		if !meta.IsStatusConditionTrue(lease.Status.Conditions,
			string(jumpstarterdevv1alpha1.LeaseConditionTypePending)) {
			continue
		}

		if lease.Spec.ExporterRef != nil && lease.Spec.ExporterRef.Name == exporter.Name {
			requests = append(requests, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: lease.Name, Namespace: lease.Namespace},
			})
			continue
		}

		selector, err := metav1.LabelSelectorAsSelector(&lease.Spec.Selector)
		if err != nil {
			continue
		}
		if selector.Matches(labels.Set(exporter.Labels)) {
			requests = append(requests, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: lease.Name, Namespace: lease.Namespace},
			})
		}
	}
	return requests
}

// SetupWithManager sets up the controller with the Manager.
func (r *LeaseReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&jumpstarterdevv1alpha1.Lease{}, builder.WithPredicates(skipEndedPredicate())).
		Watches(&jumpstarterdevv1alpha1.Exporter{},
			handler.EnqueueRequestsFromMapFunc(r.mapExporterToLeases),
			builder.WithPredicates(exporterChangedForPendingLeases())).
		Complete(r)
}
