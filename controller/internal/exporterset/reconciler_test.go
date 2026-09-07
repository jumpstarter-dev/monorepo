/*
Copyright 2026 The Jumpstarter Authors

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

package exporterset

import (
	"context"
	"strings"
	"testing"
	"time"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	virtualtargetv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/virtualtarget/v1alpha1"
	"github.com/jumpstarter-dev/jumpstarter/controller/internal/exporterset/provisioners/qemu"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const testExporterSetUID = types.UID("es-uid-1234")

const (
	kindExporterSet = "ExporterSet"
	nsDefault       = "default"
	testEndpoint    = "grpc.test.example.com:8082"
)

func newScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := virtualtargetv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme virtualtarget: %v", err)
	}
	if err := jumpstarterdevv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme jumpstarter: %v", err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme corev1: %v", err)
	}
	return scheme
}

func makeExporterSet(opts ...func(*virtualtargetv1alpha1.ExporterSet)) *virtualtargetv1alpha1.ExporterSet {
	es := &virtualtargetv1alpha1.ExporterSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "demo-set",
			Namespace:  nsDefault,
			UID:        testExporterSetUID,
			Generation: 1,
		},
		Spec: virtualtargetv1alpha1.ExporterSetSpec{
			MinReplicas:            2,
			MaxReplicas:            10,
			MinAvailableReplicas:   1,
			VirtualTargetClassName: "qemu-class",
			Selector: metav1.LabelSelector{
				MatchLabels: map[string]string{"exporterset": "demo-set"},
			},
			Template: virtualtargetv1alpha1.ExporterSetTemplate{
				Metadata: virtualtargetv1alpha1.EmbeddedObjectMeta{
					Labels: map[string]string{"exporterset": "demo-set"},
				},
			},
		},
	}
	for _, opt := range opts {
		opt(es)
	}
	return es
}

func makeVTC() *virtualtargetv1alpha1.VirtualTargetClass {
	return &virtualtargetv1alpha1.VirtualTargetClass{
		ObjectMeta: metav1.ObjectMeta{Name: "qemu-class", Namespace: nsDefault},
		Spec: virtualtargetv1alpha1.VirtualTargetClassSpec{
			Provisioner: qemu.ProvisionerName,
		},
	}
}

func makeExporter(name string, online bool, leased bool, enabled bool) *jumpstarterdevv1alpha1.Exporter {
	exp := &jumpstarterdevv1alpha1.Exporter{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: nsDefault,
			Labels:    map[string]string{"exporterset": "demo-set"},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "virtualtarget.jumpstarter.dev/v1alpha1",
				Kind:       kindExporterSet,
				Name:       "demo-set",
				UID:        testExporterSetUID,
				Controller: boolPtr(true),
			}},
		},
		Spec: jumpstarterdevv1alpha1.ExporterSpec{
			Enabled: boolPtr(enabled),
		},
	}

	onlineStatus := metav1.ConditionFalse
	if online {
		onlineStatus = metav1.ConditionTrue
	}
	exp.Status.Conditions = []metav1.Condition{{
		Type:   string(jumpstarterdevv1alpha1.ExporterConditionTypeOnline),
		Status: onlineStatus,
	}}

	if leased {
		exp.Status.LeaseRef = &corev1.LocalObjectReference{Name: name + "-lease"}
	}

	return exp
}

func newReconciler(t *testing.T, objs ...client.Object) (*ExporterSetReconciler, client.Client) {
	t.Helper()
	scheme := newScheme(t)
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(objs...).
		WithStatusSubresource(&virtualtargetv1alpha1.ExporterSet{}, &jumpstarterdevv1alpha1.Exporter{}).
		Build()
	r := &ExporterSetReconciler{
		Client:      c,
		Scheme:      scheme,
		Provisioner: qemu.New("dev"),
	}
	return r, c
}

func reconcileOnce(t *testing.T, r *ExporterSetReconciler) reconcile.Result {
	t.Helper()
	result, err := r.Reconcile(context.Background(), reconcile.Request{
		NamespacedName: types.NamespacedName{Name: "demo-set", Namespace: nsDefault},
	})
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	return result
}

func getExporterSet(t *testing.T, c client.Client) virtualtargetv1alpha1.ExporterSet {
	t.Helper()
	var es virtualtargetv1alpha1.ExporterSet
	if err := c.Get(context.Background(),
		types.NamespacedName{Name: "demo-set", Namespace: nsDefault}, &es); err != nil {
		t.Fatalf("Get ExporterSet error = %v", err)
	}
	return es
}

func listExporters(t *testing.T, c client.Client) []jumpstarterdevv1alpha1.Exporter {
	t.Helper()
	var list jumpstarterdevv1alpha1.ExporterList
	if err := c.List(context.Background(), &list, client.InNamespace(nsDefault)); err != nil {
		t.Fatalf("List Exporters error = %v", err)
	}
	return list.Items
}

func listPods(t *testing.T, c client.Client) []corev1.Pod {
	t.Helper()
	var list corev1.PodList
	if err := c.List(context.Background(), &list, client.InNamespace(nsDefault)); err != nil {
		t.Fatalf("List Pods error = %v", err)
	}
	return list.Items
}

func assertInt32(t *testing.T, field string, got, want int32) {
	t.Helper()
	if got != want {
		t.Errorf("%s = %d, want %d", field, got, want)
	}
}

func makePod(name string, phase corev1.PodPhase) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: nsDefault,
			Labels: map[string]string{
				"exporterset":        "demo-set",
				labelExporterSetName: "demo-set",
			},
			// Pods are owned by Exporters, not ExporterSets directly.
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "jumpstarter.dev/v1alpha1",
				Kind:       kindExporter,
				Name:       name,
				UID:        types.UID(name + "-uid"),
				Controller: boolPtr(true),
			}},
		},
		Status: corev1.PodStatus{Phase: phase},
	}
}

func reconcileAndGet(t *testing.T, objs ...client.Object) virtualtargetv1alpha1.ExporterSet {
	t.Helper()
	scheme := newScheme(t)
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(objs...).
		WithStatusSubresource(&virtualtargetv1alpha1.ExporterSet{}, &jumpstarterdevv1alpha1.Exporter{}).
		Build()
	r := &ExporterSetReconciler{
		Client:      c,
		Scheme:      scheme,
		Provisioner: qemu.New("dev"),
	}

	_, err := r.Reconcile(context.Background(), reconcile.Request{
		NamespacedName: types.NamespacedName{Name: "demo-set", Namespace: nsDefault},
	})
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	var updated virtualtargetv1alpha1.ExporterSet
	if err := c.Get(context.Background(),
		types.NamespacedName{Name: "demo-set", Namespace: nsDefault}, &updated); err != nil {
		t.Fatalf("Get() error = %v", err)
	}
	return updated
}

// --- VTC resolution tests ---

func TestReconcile_virtualTargetClassNotFound(t *testing.T) {
	r, _ := newReconciler(t, makeExporterSet())
	result := reconcileOnce(t, r)
	if result.RequeueAfter != 0 {
		t.Fatalf("Reconcile() result = %#v, want no requeue", result)
	}
}

func TestReconcile_provisionerMismatch(t *testing.T) {
	vtc := &virtualtargetv1alpha1.VirtualTargetClass{
		ObjectMeta: metav1.ObjectMeta{Name: "other-class", Namespace: nsDefault},
		Spec: virtualtargetv1alpha1.VirtualTargetClassSpec{
			Provisioner: "other.jumpstarter.dev",
		},
	}
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.VirtualTargetClassName = "other-class"
	})
	r, _ := newReconciler(t, vtc, es)
	result := reconcileOnce(t, r)
	if result.RequeueAfter != 0 {
		t.Fatalf("Reconcile() result = %#v, want no requeue", result)
	}
}

// --- Scale-up tests ---

func TestReconcile_scaleUp_noExporters_createsMinReplicas(t *testing.T) {
	caCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      caConfigMapName,
			Namespace: nsDefault,
		},
		Data: map[string]string{
			caConfigMapKey: "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
		},
	}
	r, c := newReconciler(t, makeExporterSet(), makeVTC(), caCM)

	// Phase 1: minReplicas=2, one Exporter per reconcile.
	for range 2 {
		reconcileOnce(t, r)
	}

	exporters := listExporters(t, c)
	if len(exporters) != 2 {
		t.Fatalf("expected 2 Exporters (minReplicas), got %d", len(exporters))
	}

	// No Pods yet — credentials not ready.
	pods := listPods(t, c)
	if len(pods) != 0 {
		t.Fatalf("expected 0 Pods before credentials, got %d", len(pods))
	}

	for _, exp := range exporters {
		if !isOwnedBy(&exp, testExporterSetUID) {
			t.Errorf("Exporter %s not owned by ExporterSet", exp.Name)
		}
		if !exp.IsEnabled() {
			t.Errorf("Exporter %s should be enabled", exp.Name)
		}
	}

	// Phase 2: simulate credentials and endpoint on each Exporter.
	for i := range exporters {
		exp := &exporters[i]
		credSecret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "cred-" + exp.Name,
				Namespace: nsDefault,
			},
			Data: map[string][]byte{
				"token": []byte("test-token-" + exp.Name),
			},
		}
		if err := c.Create(context.Background(), credSecret); err != nil {
			t.Fatalf("create credential Secret: %v", err)
		}
		exp.Status.Credential = &corev1.LocalObjectReference{Name: credSecret.Name}
		exp.Status.Endpoint = testEndpoint
		if err := c.Status().Update(context.Background(), exp); err != nil {
			t.Fatalf("update Exporter status: %v", err)
		}
	}

	reconcileOnce(t, r)

	pods = listPods(t, c)
	if len(pods) != 2 {
		t.Fatalf("expected 2 Pods after credentials, got %d", len(pods))
	}

	for _, pod := range pods {
		ownedByExporter := false
		for _, ref := range pod.OwnerReferences {
			if ref.Kind == kindExporter && ref.Controller != nil && *ref.Controller {
				ownedByExporter = true
				break
			}
		}
		if !ownedByExporter {
			t.Errorf("Pod %s should be owned by an Exporter", pod.Name)
		}

		ownedByES := false
		for _, ref := range pod.OwnerReferences {
			if ref.UID == testExporterSetUID {
				ownedByES = true
			}
		}
		if ownedByES {
			t.Errorf("Pod %s should NOT be directly owned by ExporterSet", pod.Name)
		}

		if pod.Labels[labelExporterSetName] != "demo-set" {
			t.Errorf("Pod %s missing ExporterSet label", pod.Name)
		}
	}
}

func TestReconcile_scaleUp_warmBuffer(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 3
	})
	r, c := newReconciler(t, es, makeVTC())

	// minAvailableReplicas=3: one Exporter per reconcile.
	for range 3 {
		reconcileOnce(t, r)
	}

	exporters := listExporters(t, c)
	if len(exporters) != 3 {
		t.Fatalf("expected 3 Exporters (minAvailableReplicas), got %d", len(exporters))
	}
}

func TestReconcile_scaleUp_respectsMaxReplicas(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 5
		es.Spec.MaxReplicas = 3
	})
	r, c := newReconciler(t, es, makeVTC())

	// maxReplicas=3: 3 reconciles create 3, the 4th is a no-op (capped).
	for range 4 {
		reconcileOnce(t, r)
	}

	exporters := listExporters(t, c)
	if len(exporters) != 3 {
		t.Fatalf("expected 3 Exporters (capped by maxReplicas), got %d", len(exporters))
	}
}

func TestReconcile_scaleUp_refillsWarmBuffer(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 2
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, true, true),  // online, leased
		makeExporter("exp-2", true, false, true), // online, not leased (available=1)
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 3 {
		t.Fatalf("expected 3 Exporters (refilled warm buffer), got %d", len(exporters))
	}
}

func TestReconcile_scaleUp_noOpWhenSufficient(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 2
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 2 {
		t.Fatalf("expected 2 Exporters (no change), got %d", len(exporters))
	}
}

func TestReconcile_scaleUp_atMaxReplicas_noOp(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 5
		es.Spec.MaxReplicas = 2
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, true, true),
		makeExporter("exp-2", true, true, true),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 2 {
		t.Fatalf("expected 2 Exporters (at maxReplicas), got %d", len(exporters))
	}
}

// --- Scale-down tests ---

func TestReconcile_scaleDown_setsAnnotationAndRequeues(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 1
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makeExporter("exp-3", true, false, true),
	)

	result := reconcileOnce(t, r)

	if result.RequeueAfter == 0 {
		t.Fatal("expected RequeueAfter for cooldown, got 0")
	}

	updated := getExporterSet(t, c)
	if _, ok := updated.Annotations[annotationSurplusSince]; !ok {
		t.Fatal("expected surplus-since annotation to be set")
	}

	exporters := listExporters(t, c)
	if len(exporters) != 3 {
		t.Fatalf("expected 3 Exporters (cooldown not elapsed), got %d", len(exporters))
	}
}

func TestReconcile_scaleDown_afterCooldown_disablesExporter(t *testing.T) {
	cooldown := 1 * time.Second
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 1
		es.Spec.MinAvailableReplicas = 1
		es.Spec.ScaleDownCooldown = &metav1.Duration{Duration: cooldown}
		es.Annotations = map[string]string{
			annotationSurplusSince: time.Now().Add(-2 * cooldown).UTC().Format(time.RFC3339),
		}
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makeExporter("exp-3", true, false, true),
	)

	result := reconcileOnce(t, r)

	// After disabling an exporter the controller resets the annotation and
	// returns RequeueAfter: cooldown (not an immediate Requeue).
	if result.RequeueAfter == 0 {
		t.Fatal("expected RequeueAfter > 0 after disabling exporter")
	}

	exporters := listExporters(t, c)
	disabledCount := 0
	for _, exp := range exporters {
		if !exp.IsEnabled() {
			disabledCount++
		}
	}
	if disabledCount != 1 {
		t.Fatalf("expected 1 disabled Exporter, got %d", disabledCount)
	}
}

func TestReconcile_scaleDown_deletesDisabledUnleasedExporter(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 1
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makeExporter("exp-disabled", true, false, false), // disabled, unleased -> cleaned up
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 2 {
		t.Fatalf("expected 2 Exporters after cleanup, got %d", len(exporters))
	}

	for _, exp := range exporters {
		if exp.Name == "exp-disabled" {
			t.Fatal("disabled exporter should have been cleaned up")
		}
	}
}

func TestReconcile_scaleDown_doesNotDeleteLeasedDisabledExporter(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 1
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makeExporter("exp-disabled-leased", true, true, false), // disabled but still leased
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 3 {
		t.Fatalf("expected 3 Exporters (leased disabled not deleted), got %d", len(exporters))
	}
}

func TestReconcile_exitAndReplace_deletesSucceededUnleasedExporter(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MaxReplicas = 1
		es.Spec.MinAvailableReplicas = 1
		es.Spec.RecycleStrategy = virtualtargetv1alpha1.RecycleStrategyExitAndReplace
	})

	// Offline exporter with Succeeded Pod (exitOnLeaseEnd completed).
	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-dead", false, false, true),
		makePod("exp-dead", corev1.PodSucceeded),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 0 {
		t.Fatalf("expected Succeeded exporter deleted for ExitAndReplace, got %d", len(exporters))
	}
}

func TestReconcile_exitAndReplace_deletesFailedUnleasedExporter(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MaxReplicas = 1
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-crash", false, false, true),
		makePod("exp-crash", corev1.PodFailed),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 0 {
		t.Fatalf("expected Failed exporter deleted, got %d", len(exporters))
	}
}

func TestReconcile_exitAndReplace_keepsRunningExporter(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MaxReplicas = 1
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-live", true, false, true),
		makePod("exp-live", corev1.PodRunning),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected Running exporter kept, got %d", len(exporters))
	}
}

func TestReconcile_exitAndReplace_keepsLeasedTerminalExporter(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MaxReplicas = 1
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-leased-dead", false, true, true),
		makePod("exp-leased-dead", corev1.PodSucceeded),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected leased terminal exporter kept until lease clears, got %d", len(exporters))
	}
}

func TestReconcile_inPlaceReuse_keepsFailedUnleasedExporter(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MaxReplicas = 1
		es.Spec.MinAvailableReplicas = 1
		es.Spec.RecycleStrategy = virtualtargetv1alpha1.RecycleStrategyInPlaceReuse
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-oom", false, false, true),
		makePod("exp-oom", corev1.PodFailed),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected InPlaceReuse Failed exporter kept, got %d", len(exporters))
	}
}

func TestReconcile_exitAndReplace_keepsExporterWithNoPods(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MaxReplicas = 1
		es.Spec.MinAvailableReplicas = 1
		es.Spec.RecycleStrategy = virtualtargetv1alpha1.RecycleStrategyExitAndReplace
	})

	// Fresh exporter awaiting Pod creation must not be deleted: allPodsTerminal
	// returns false for an empty pod slice.
	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-new", false, false, true),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected exporter with no pods kept, got %d", len(exporters))
	}
}

func TestAllPodsTerminal_emptyReturnsFalse(t *testing.T) {
	if allPodsTerminal(nil) {
		t.Fatal("allPodsTerminal(nil) = true, want false")
	}
	if allPodsTerminal([]corev1.Pod{}) {
		t.Fatal("allPodsTerminal([]) = true, want false")
	}
}

func TestReconcile_exitAndReplace_maxReplicasRefillsAfterDelete(t *testing.T) {
	caCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      caConfigMapName,
			Namespace: nsDefault,
		},
		Data: map[string]string{
			caConfigMapKey: "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
		},
	}
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MaxReplicas = 1
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(), caCM,
		makeExporter("exp-dead", false, false, true),
		makePod("exp-dead", corev1.PodSucceeded),
	)

	// First reconcile: delete terminal exporter (frees the maxReplicas slot).
	reconcileOnce(t, r)
	if got := len(listExporters(t, c)); got != 0 {
		t.Fatalf("after cleanup: expected 0 exporters, got %d", got)
	}

	// Second reconcile: warm buffer scale-up creates a replacement.
	reconcileOnce(t, r)
	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("after refill: expected 1 exporter, got %d", len(exporters))
	}
	if exporters[0].Name == "exp-dead" {
		t.Fatal("replacement should be a new Exporter, not exp-dead")
	}
}

func TestReconcile_scaleDown_respectsMinReplicas(t *testing.T) {
	cooldown := 1 * time.Second
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 3
		es.Spec.MinAvailableReplicas = 0
		es.Spec.ScaleDownCooldown = &metav1.Duration{Duration: cooldown}
		es.Annotations = map[string]string{
			annotationSurplusSince: time.Now().Add(-2 * cooldown).UTC().Format(time.RFC3339),
		}
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makeExporter("exp-3", true, false, true),
	)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 3 {
		t.Fatalf("expected 3 Exporters (at minReplicas), got %d", len(exporters))
	}
}

// --- Ownership chain tests ---

func TestReconcile_ownershipChain_exporterOwnedByExporterSet(t *testing.T) {
	r, c := newReconciler(t, makeExporterSet(), makeVTC())

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	for _, exp := range exporters {
		found := false
		for _, ref := range exp.OwnerReferences {
			if ref.UID == testExporterSetUID &&
				ref.Kind == kindExporterSet &&
				ref.Controller != nil && *ref.Controller {
				found = true
			}
		}
		if !found {
			t.Errorf("Exporter %s should be controller-owned by ExporterSet", exp.Name)
		}
	}
}

func TestReconcile_ownershipChain_podOwnedByExporter(t *testing.T) {
	caCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      caConfigMapName,
			Namespace: nsDefault,
		},
		Data: map[string]string{
			caConfigMapKey: "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
		},
	}
	r, c := newReconciler(t, makeExporterSet(), makeVTC(), caCM)

	// Phase 1: create Exporters.
	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) == 0 {
		t.Fatal("expected at least 1 Exporter")
	}

	// Phase 2: simulate credentials.
	for i := range exporters {
		exp := &exporters[i]
		credSecret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "cred-" + exp.Name,
				Namespace: nsDefault,
			},
			Data: map[string][]byte{
				"token": []byte("test-token-" + exp.Name),
			},
		}
		if err := c.Create(context.Background(), credSecret); err != nil {
			t.Fatalf("create credential Secret: %v", err)
		}
		exp.Status.Credential = &corev1.LocalObjectReference{Name: credSecret.Name}
		exp.Status.Endpoint = testEndpoint
		if err := c.Status().Update(context.Background(), exp); err != nil {
			t.Fatalf("update Exporter status: %v", err)
		}
	}

	reconcileOnce(t, r)

	// Re-read exporters after reconcile.
	exporters = listExporters(t, c)
	pods := listPods(t, c)

	if len(pods) == 0 {
		t.Fatal("expected at least 1 Pod after credentials are ready")
	}

	exporterUIDs := make(map[types.UID]bool)
	for _, exp := range exporters {
		exporterUIDs[exp.UID] = true
	}

	for _, pod := range pods {
		ownedByExporter := false
		for _, ref := range pod.OwnerReferences {
			if ref.Kind == kindExporter &&
				ref.Controller != nil && *ref.Controller &&
				exporterUIDs[ref.UID] {
				ownedByExporter = true
			}
		}
		if !ownedByExporter {
			t.Errorf("Pod %s should be controller-owned by an Exporter", pod.Name)
		}

		ownedByES := false
		for _, ref := range pod.OwnerReferences {
			if ref.UID == testExporterSetUID {
				ownedByES = true
			}
		}
		if ownedByES {
			t.Errorf("Pod %s should NOT be directly owned by ExporterSet", pod.Name)
		}
	}
}

// --- Pod watch mapping tests ---

func TestFindExporterSetForPod_returnsRequestForLabeledPod(t *testing.T) {
	r, _ := newReconciler(t)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pod",
			Namespace: nsDefault,
			Labels:    map[string]string{labelExporterSetName: "my-set"},
		},
	}

	requests := r.findExporterSetForPod(context.Background(), pod)
	if len(requests) != 1 {
		t.Fatalf("got %d requests, want 1", len(requests))
	}
	if requests[0].Name != "my-set" || requests[0].Namespace != nsDefault {
		t.Errorf("request = %#v, want my-set/default", requests[0])
	}
}

func TestFindExporterSetForPod_returnsNilForUnlabeledPod(t *testing.T) {
	r, _ := newReconciler(t)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "unrelated-pod",
			Namespace: nsDefault,
		},
	}

	requests := r.findExporterSetForPod(context.Background(), pod)
	if len(requests) != 0 {
		t.Errorf("got %d requests, want 0", len(requests))
	}
}

func TestFindExporterSetForPod_returnsNilForNonPod(t *testing.T) {
	r, _ := newReconciler(t)
	requests := r.findExporterSetForPod(context.Background(), &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "cm", Namespace: nsDefault},
	})
	if requests != nil {
		t.Errorf("got %#v, want nil", requests)
	}
}

// --- VTC watch mapping tests ---

func TestFindExporterSetsForVTC_filtersByClassName(t *testing.T) {
	vtc := makeVTC()
	matching := &virtualtargetv1alpha1.ExporterSet{
		ObjectMeta: metav1.ObjectMeta{Name: "matching", Namespace: nsDefault},
		Spec: virtualtargetv1alpha1.ExporterSetSpec{
			VirtualTargetClassName: "qemu-class",
		},
	}
	other := &virtualtargetv1alpha1.ExporterSet{
		ObjectMeta: metav1.ObjectMeta{Name: "other", Namespace: nsDefault},
		Spec: virtualtargetv1alpha1.ExporterSetSpec{
			VirtualTargetClassName: "other-class",
		},
	}
	r, _ := newReconciler(t, vtc, matching, other)

	requests := r.findExporterSetsForVTC(context.Background(), vtc)
	if len(requests) != 1 {
		t.Fatalf("got %d requests, want 1: %#v", len(requests), requests)
	}
	if requests[0].Name != "matching" || requests[0].Namespace != nsDefault {
		t.Errorf("request = %#v, want matching/default", requests[0])
	}
}

func TestFindExporterSetsForVTC_ignoresNonVTC(t *testing.T) {
	r, _ := newReconciler(t)
	requests := r.findExporterSetsForVTC(context.Background(), &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "pod", Namespace: nsDefault},
	})
	if requests != nil {
		t.Errorf("got %#v, want nil", requests)
	}
}

// --- computePoolState tests ---

func TestComputePoolState(t *testing.T) {
	exporters := []jumpstarterdevv1alpha1.Exporter{
		*makeExporter("e1", true, false, true),  // online, unleased, enabled -> available, ready
		*makeExporter("e2", true, true, true),   // online, leased, enabled -> ready, leased
		*makeExporter("e3", false, false, true), // offline, unleased, enabled
		*makeExporter("e4", true, false, false), // online, unleased, disabled -> ready (not available)
	}

	state := computePoolState(exporters)
	assertInt32(t, "replicas", state.replicas, 4)
	assertInt32(t, "ready", state.ready, 3)
	assertInt32(t, "available", state.available, 1)
	assertInt32(t, "leased", state.leased, 1)
}

// --- Deep merge tests ---

func TestDeepMerge_mapsRecursive(t *testing.T) {
	base := map[string]interface{}{
		"resources": map[string]interface{}{
			"cpu":     "4",
			"memory":  "4Gi",
			"storage": "16Gi",
		},
		"firmware": map[string]interface{}{
			"url": "registry.example.com/fw:v1",
		},
	}
	override := map[string]interface{}{
		"resources": map[string]interface{}{
			"memory": "8Gi",
		},
	}

	result := deepMerge(base, override)

	resources := result["resources"].(map[string]interface{})
	if resources["cpu"] != "4" {
		t.Errorf("cpu = %v, want 4", resources["cpu"])
	}
	if resources["memory"] != "8Gi" {
		t.Errorf("memory = %v, want 8Gi", resources["memory"])
	}
	if resources["storage"] != "16Gi" {
		t.Errorf("storage = %v, want 16Gi", resources["storage"])
	}

	firmware := result["firmware"].(map[string]interface{})
	if firmware["url"] != "registry.example.com/fw:v1" {
		t.Errorf("firmware.url = %v, want original", firmware["url"])
	}
}

func TestDeepMerge_scalarReplace(t *testing.T) {
	base := map[string]interface{}{"machineType": "virt"}
	override := map[string]interface{}{"machineType": "q35"}

	result := deepMerge(base, override)
	if result["machineType"] != "q35" {
		t.Errorf("machineType = %v, want q35", result["machineType"])
	}
}

func TestDeepMerge_listReplace(t *testing.T) {
	base := map[string]interface{}{"ports": []interface{}{22, 80}}
	override := map[string]interface{}{"ports": []interface{}{443}}

	result := deepMerge(base, override)
	ports := result["ports"].([]interface{})
	if len(ports) != 1 || ports[0] != 443 {
		t.Errorf("ports = %v, want [443]", ports)
	}
}

// --- Status update tests ---

func TestReconcile_statusUpdated(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, true, true),
		makeExporter("exp-3", false, false, true),
	)

	reconcileOnce(t, r)

	updated := getExporterSet(t, c)
	assertInt32(t, "Replicas", updated.Status.Replicas, 3)
	assertInt32(t, "ReadyReplicas", updated.Status.ReadyReplicas, 2)
	assertInt32(t, "AvailableReplicas", updated.Status.AvailableReplicas, 1)
	assertInt32(t, "LeasedReplicas", updated.Status.LeasedReplicas, 1)
}

// --- Idempotency test ---

func TestReconcile_idempotent(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 2
		es.Spec.MinAvailableReplicas = 1
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, true, true),
	)

	reconcileOnce(t, r)
	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 2 {
		t.Fatalf("expected 2 Exporters after idempotent reconcile, got %d", len(exporters))
	}
}

// --- In-memory race guard test ---

func TestReconcile_scaleDown_inMemoryGuardPreventsRapidDisables(t *testing.T) {
	// Long cooldown so only the in-memory guard (not the annotation check)
	// can block the third reconcile.
	cooldown := 10 * time.Second
	pastTimestamp := time.Now().Add(-2 * cooldown).UTC().Format(time.RFC3339)

	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
		es.Spec.ScaleDownCooldown = &metav1.Duration{Duration: cooldown}
		es.Annotations = map[string]string{
			annotationSurplusSince: pastTimestamp,
		}
	})

	r, c := newReconciler(t,
		es, makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
	)

	// Reconcile 1: cooldown elapsed → disables exp-1, records lastScaleDownAction.
	reconcileOnce(t, r)

	// Reconcile 2: cleanupDisabledExporters deletes exp-1, returns early.
	// The stale annotation doesn't matter here because cleanup short-circuits.
	reconcileOnce(t, r)

	// Simulate cache lag before reconcile 3: reset the annotation back to the
	// stale past timestamp, as if the informer hasn't seen setSurplusAnnotation.
	var fresh virtualtargetv1alpha1.ExporterSet
	if err := c.Get(context.Background(),
		types.NamespacedName{Name: "demo-set", Namespace: nsDefault}, &fresh); err != nil {
		t.Fatalf("Get ExporterSet: %v", err)
	}
	if fresh.Annotations == nil {
		fresh.Annotations = make(map[string]string)
	}
	fresh.Annotations[annotationSurplusSince] = pastTimestamp
	if err := c.Update(context.Background(), &fresh); err != nil {
		t.Fatalf("Reset annotation: %v", err)
	}

	// Reconcile 3: only exp-2 remains. Annotation appears elapsed again, but
	// the in-memory guard (lastScaleDownAction < cooldown ago) must block it.
	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected 1 exporter remaining, got %d", len(exporters))
	}
	if !exporters[0].IsEnabled() {
		t.Fatal("in-memory guard should have blocked disabling the remaining exporter")
	}
}

func TestReconcile_ignoresUnownedExporters(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
	})

	unowned := makeExporter("unowned", true, false, true)
	unowned.OwnerReferences = nil

	r, c := newReconciler(t, es, makeVTC(), unowned)

	reconcileOnce(t, r)

	updated := getExporterSet(t, c)
	assertInt32(t, "Replicas", updated.Status.Replicas, 0)
}

// --- Demand-driven scale-up tests ---

func makePendingLease(name string, selectorLabels map[string]string) *jumpstarterdevv1alpha1.Lease {
	return &jumpstarterdevv1alpha1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: nsDefault,
		},
		Spec: jumpstarterdevv1alpha1.LeaseSpec{
			ClientRef: corev1.LocalObjectReference{Name: "test-client"},
			Selector: metav1.LabelSelector{
				MatchLabels: selectorLabels,
			},
		},
		Status: jumpstarterdevv1alpha1.LeaseStatus{
			Ended: false,
			Conditions: []metav1.Condition{{
				Type:   string(jumpstarterdevv1alpha1.LeaseConditionTypePending),
				Status: metav1.ConditionTrue,
			}},
		},
	}
}

func TestReconcile_demandDriven_scalesUpForPendingLease(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
		es.Spec.MaxReplicas = 5
	})

	// One pending lease matching the ExporterSet's template labels.
	lease := makePendingLease("pending-1", map[string]string{"exporterset": "demo-set"})

	r, c := newReconciler(t, es, makeVTC(), lease)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected 1 Exporter (demand-driven scale-up), got %d", len(exporters))
	}
}

func TestReconcile_demandDriven_noScaleUpWhenAvailableExists(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
		es.Spec.MaxReplicas = 5
	})

	// A pending lease, but there's already an available exporter.
	lease := makePendingLease("pending-1", map[string]string{"exporterset": "demo-set"})
	exp := makeExporter("exp-1", true, false, true) // online, unleased = available

	r, c := newReconciler(t, es, makeVTC(), lease, exp)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected 1 Exporter (no scale-up, available exists), got %d", len(exporters))
	}
}

func TestReconcile_demandDriven_respectsMaxReplicas(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
		es.Spec.MaxReplicas = 2
	})

	// Three pending leases but maxReplicas=2 caps us.
	lease1 := makePendingLease("pending-1", map[string]string{"exporterset": "demo-set"})
	lease2 := makePendingLease("pending-2", map[string]string{"exporterset": "demo-set"})
	lease3 := makePendingLease("pending-3", map[string]string{"exporterset": "demo-set"})

	r, c := newReconciler(t, es, makeVTC(), lease1, lease2, lease3)

	// 3 reconciles: first 2 create, 3rd is capped.
	for range 3 {
		reconcileOnce(t, r)
	}

	exporters := listExporters(t, c)
	if len(exporters) != 2 {
		t.Fatalf("expected 2 Exporters (capped by maxReplicas), got %d", len(exporters))
	}
}

func TestReconcile_demandDriven_ignoresNonMatchingLeases(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
		es.Spec.MaxReplicas = 5
	})

	// Pending lease with different labels — shouldn't match.
	lease := makePendingLease("pending-other", map[string]string{"exporterset": "other-pool"})

	r, c := newReconciler(t, es, makeVTC(), lease)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 0 {
		t.Fatalf("expected 0 Exporters (lease doesn't match), got %d", len(exporters))
	}
}

func TestReconcile_demandDriven_ignoresEndedLeases(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
		es.Spec.MaxReplicas = 5
	})

	// Ended lease — shouldn't trigger scale-up.
	lease := makePendingLease("ended-1", map[string]string{"exporterset": "demo-set"})
	lease.Status.Ended = true

	r, c := newReconciler(t, es, makeVTC(), lease)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 0 {
		t.Fatalf("expected 0 Exporters (lease ended), got %d", len(exporters))
	}
}

func TestReconcile_demandDriven_ignoresAssignedLeases(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 0
		es.Spec.MinAvailableReplicas = 0
		es.Spec.MaxReplicas = 5
	})

	// Already assigned lease — shouldn't trigger scale-up.
	lease := makePendingLease("assigned-1", map[string]string{"exporterset": "demo-set"})
	lease.Status.ExporterRef = &corev1.LocalObjectReference{Name: "some-exporter"}

	r, c := newReconciler(t, es, makeVTC(), lease)

	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 0 {
		t.Fatalf("expected 0 Exporters (lease already assigned), got %d", len(exporters))
	}
}

// --- Lease watch mapping tests ---

func TestFindExporterSetsForLease_matchesTemplateLabels(t *testing.T) {
	es := makeExporterSet()
	r, _ := newReconciler(t, es, makeVTC())

	lease := &jumpstarterdevv1alpha1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-lease",
			Namespace: nsDefault,
		},
		Spec: jumpstarterdevv1alpha1.LeaseSpec{
			ClientRef: corev1.LocalObjectReference{Name: "client"},
			Selector: metav1.LabelSelector{
				MatchLabels: map[string]string{"exporterset": "demo-set"},
			},
		},
	}

	requests := r.findExporterSetsForLease(context.Background(), lease)
	if len(requests) != 1 {
		t.Fatalf("got %d requests, want 1", len(requests))
	}
	if requests[0].Name != "demo-set" {
		t.Errorf("request name = %q, want demo-set", requests[0].Name)
	}
}

func TestFindExporterSetsForLease_ignoresEndedLease(t *testing.T) {
	es := makeExporterSet()
	r, _ := newReconciler(t, es, makeVTC())

	lease := &jumpstarterdevv1alpha1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "ended-lease",
			Namespace: nsDefault,
		},
		Spec: jumpstarterdevv1alpha1.LeaseSpec{
			ClientRef: corev1.LocalObjectReference{Name: "client"},
			Selector: metav1.LabelSelector{
				MatchLabels: map[string]string{"exporterset": "demo-set"},
			},
		},
		Status: jumpstarterdevv1alpha1.LeaseStatus{
			Ended: true,
		},
	}

	requests := r.findExporterSetsForLease(context.Background(), lease)
	if len(requests) != 0 {
		t.Fatalf("got %d requests for ended lease, want 0", len(requests))
	}
}

func TestFindExporterSetsForLease_ignoresNonMatchingSelector(t *testing.T) {
	es := makeExporterSet()
	r, _ := newReconciler(t, es, makeVTC())

	lease := &jumpstarterdevv1alpha1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "other-lease",
			Namespace: nsDefault,
		},
		Spec: jumpstarterdevv1alpha1.LeaseSpec{
			ClientRef: corev1.LocalObjectReference{Name: "client"},
			Selector: metav1.LabelSelector{
				MatchLabels: map[string]string{"exporterset": "other-pool"},
			},
		},
	}

	requests := r.findExporterSetsForLease(context.Background(), lease)
	if len(requests) != 0 {
		t.Fatalf("got %d requests for non-matching lease, want 0", len(requests))
	}
}

// --- Status condition tests ---

func TestReconcile_statusCounts_allOnlineNotLeased(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makePod("pod-1", corev1.PodRunning),
		makePod("pod-2", corev1.PodRunning),
	)

	assertInt32(t, "Replicas", es.Status.Replicas, 2)
	assertInt32(t, "ReadyReplicas", es.Status.ReadyReplicas, 2)
	assertInt32(t, "AvailableReplicas", es.Status.AvailableReplicas, 2)
	assertInt32(t, "UnavailableReplicas", es.Status.UnavailableReplicas, 0)
	assertInt32(t, "LeasedReplicas", es.Status.LeasedReplicas, 0)
	assertInt32(t, "ExportersActive", es.Status.ExportersActive, 0)
	assertInt32(t, "ExportersIdle", es.Status.ExportersIdle, 2)
	assertInt32(t, "ExportersDisabled", es.Status.ExportersDisabled, 0)
	assertInt32(t, "ExportersOffline", es.Status.ExportersOffline, 0)
	assertInt32(t, "PodsRunning", es.Status.PodsRunning, 2)
	assertInt32(t, "PodsPending", es.Status.PodsPending, 0)
}

func TestReconcile_statusCounts_mixedStates(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, true, true),
		makeExporter("exp-2", true, false, true),
		makeExporter("exp-3", false, false, true),
		makeExporter("exp-4", true, true, false), // disabled but leased (draining)
		makePod("pod-1", corev1.PodRunning),
		makePod("pod-2", corev1.PodRunning),
		makePod("pod-3", corev1.PodFailed),
		makePod("pod-4", corev1.PodPending),
	)

	assertInt32(t, "Replicas", es.Status.Replicas, 4)
	assertInt32(t, "ReadyReplicas", es.Status.ReadyReplicas, 3)
	assertInt32(t, "AvailableReplicas", es.Status.AvailableReplicas, 1)
	assertInt32(t, "UnavailableReplicas", es.Status.UnavailableReplicas, 1)
	assertInt32(t, "LeasedReplicas", es.Status.LeasedReplicas, 2)
	assertInt32(t, "ExportersActive", es.Status.ExportersActive, 1)
	assertInt32(t, "ExportersIdle", es.Status.ExportersIdle, 1)
	assertInt32(t, "ExportersDisabled", es.Status.ExportersDisabled, 1)
	assertInt32(t, "ExportersOffline", es.Status.ExportersOffline, 1)
	assertInt32(t, "PodsRunning", es.Status.PodsRunning, 2)
	assertInt32(t, "PodsFailed", es.Status.PodsFailed, 1)
	assertInt32(t, "PodsPending", es.Status.PodsPending, 1)
}

func TestReconcile_conditionAvailable_meetsMinReplicas(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "Available")
	if cond == nil {
		t.Fatal("Expected Available condition")
	}
	if cond.Status != metav1.ConditionTrue {
		t.Errorf("Available = %v, want True", cond.Status)
	}
	if cond.Reason != "MinimumReplicasAvailable" {
		t.Errorf("Reason = %q, want MinimumReplicasAvailable", cond.Reason)
	}
}

func TestReconcile_conditionAvailable_belowMinReplicas(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "Available")
	if cond == nil {
		t.Fatal("Expected Available condition")
	}
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("Available = %v, want False", cond.Status)
	}
	if cond.Reason != "MinimumReplicasUnavailable" {
		t.Errorf("Reason = %q, want MinimumReplicasUnavailable", cond.Reason)
	}
}

func TestReconcile_conditionProgressing_podsPending(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makePod("pod-1", corev1.PodRunning),
		makePod("pod-2", corev1.PodPending),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "Progressing")
	if cond == nil {
		t.Fatal("Expected Progressing condition")
	}
	if cond.Status != metav1.ConditionTrue {
		t.Errorf("Progressing = %v, want True", cond.Status)
	}
	if cond.Reason != "PodsStarting" {
		t.Errorf("Reason = %q, want PodsStarting", cond.Reason)
	}
}

func TestReconcile_conditionProgressing_allReady(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "Progressing")
	if cond == nil {
		t.Fatal("Expected Progressing condition")
	}
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("Progressing = %v, want False", cond.Status)
	}
}

func TestReconcile_conditionDegraded_failedPods(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", false, false, true),
		makePod("pod-1", corev1.PodRunning),
		makePod("pod-2", corev1.PodFailed),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "Degraded")
	if cond == nil {
		t.Fatal("Expected Degraded condition")
	}
	if cond.Status != metav1.ConditionTrue {
		t.Errorf("Degraded = %v, want True", cond.Status)
	}
	if cond.Reason != "PodsFailing" {
		t.Errorf("Reason = %q, want PodsFailing", cond.Reason)
	}
}

func TestReconcile_conditionDegraded_healthy(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makePod("pod-1", corev1.PodRunning),
		makePod("pod-2", corev1.PodRunning),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "Degraded")
	if cond == nil {
		t.Fatal("Expected Degraded condition")
	}
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("Degraded = %v, want False", cond.Status)
	}
}

func TestReconcile_conditionScalingLimited_atMax(t *testing.T) {
	esObj := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MaxReplicas = 2
		es.Spec.MinAvailableReplicas = 2
	})

	es := reconcileAndGet(t,
		esObj,
		makeVTC(),
		makeExporter("exp-1", true, true, true),
		makeExporter("exp-2", true, true, true),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "ScalingLimited")
	if cond == nil {
		t.Fatal("Expected ScalingLimited condition")
	}
	if cond.Status != metav1.ConditionTrue {
		t.Errorf("ScalingLimited = %v, want True", cond.Status)
	}
	if cond.Reason != "AtMaxReplicas" {
		t.Errorf("Reason = %q, want AtMaxReplicas", cond.Reason)
	}
}

func TestReconcile_conditionScalingLimited_withinLimits(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
	)

	cond := meta.FindStatusCondition(es.Status.Conditions, "ScalingLimited")
	if cond == nil {
		t.Fatal("Expected ScalingLimited condition")
	}
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("ScalingLimited = %v, want False", cond.Status)
	}
}

func TestReconcile_selectorField_populated(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
	)

	if es.Status.Selector != "exporterset=demo-set" {
		t.Errorf("Selector = %q, want %q", es.Status.Selector, "exporterset=demo-set")
	}
}

func TestReconcile_ignoresUnownedPods(t *testing.T) {
	unowned := makePod("unowned-pod", corev1.PodRunning)
	unowned.OwnerReferences = nil

	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		unowned,
	)

	assertInt32(t, "PodsRunning", es.Status.PodsRunning, 0)
}

func TestReconcile_offlineLeasedExporter_notCountedAsActive(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-online-idle", true, false, true),
		makeExporter("exp-online-leased", true, true, true),
		makeExporter("exp-offline-leased", false, true, true),
	)

	assertInt32(t, "ExportersActive", es.Status.ExportersActive, 1)
	assertInt32(t, "ExportersOffline", es.Status.ExportersOffline, 1)
	assertInt32(t, "ExportersIdle", es.Status.ExportersIdle, 1)
	assertInt32(t, "LeasedReplicas", es.Status.LeasedReplicas, 2)
	assertInt32(t, "UnavailableReplicas", es.Status.UnavailableReplicas, 1)
}

func TestReconcile_disabledExporters_dontInflateUnavailable(t *testing.T) {
	es := reconcileAndGet(t,
		makeExporterSet(),
		makeVTC(),
		makeExporter("exp-1", true, false, true),
		makeExporter("exp-2", true, false, true),
		makeExporter("exp-disabled-offline", false, true, false), // disabled but leased (draining)
		makeExporter("exp-disabled-online", true, true, false),   // disabled but leased (draining)
	)

	assertInt32(t, "Replicas", es.Status.Replicas, 4)
	assertInt32(t, "ReadyReplicas", es.Status.ReadyReplicas, 3)
	assertInt32(t, "UnavailableReplicas", es.Status.UnavailableReplicas, 0)
	assertInt32(t, "ExportersDisabled", es.Status.ExportersDisabled, 2)

	cond := meta.FindStatusCondition(es.Status.Conditions, "Progressing")
	if cond == nil {
		t.Fatal("Expected Progressing condition")
	}
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("Progressing = %v, want False (disabled exporters should not trigger)", cond.Status)
	}

	degraded := meta.FindStatusCondition(es.Status.Conditions, "Degraded")
	if degraded == nil {
		t.Fatal("Expected Degraded condition")
	}
	if degraded.Status != metav1.ConditionFalse {
		t.Errorf("Degraded = %v, want False (disabled exporters should not trigger)", degraded.Status)
	}
}

func TestReconcile_vtcNotFound_updatesAllConditionsAndCounters(t *testing.T) {
	scheme := newScheme(t)
	esObj := makeExporterSet()
	exp1 := makeExporter("exp-1", true, false, true)
	exp2 := makeExporter("exp-2", false, false, true)

	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(esObj, makeVTC(), exp1, exp2).
		WithStatusSubresource(&virtualtargetv1alpha1.ExporterSet{}, &jumpstarterdevv1alpha1.Exporter{}).
		Build()
	r := &ExporterSetReconciler{
		Client:      c,
		Scheme:      scheme,
		Provisioner: qemu.New("dev"),
	}
	req := reconcile.Request{
		NamespacedName: types.NamespacedName{Name: "demo-set", Namespace: nsDefault},
	}

	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("first Reconcile() error = %v", err)
	}

	// Delete VTC to trigger not-found path
	vtc := makeVTC()
	if err := c.Delete(context.Background(), vtc); err != nil {
		t.Fatalf("Delete VTC error = %v", err)
	}

	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("second Reconcile() error = %v", err)
	}

	var es virtualtargetv1alpha1.ExporterSet
	if err := c.Get(context.Background(),
		types.NamespacedName{Name: "demo-set", Namespace: nsDefault}, &es); err != nil {
		t.Fatalf("Get() error = %v", err)
	}

	avail := meta.FindStatusCondition(es.Status.Conditions, "Available")
	if avail == nil {
		t.Fatal("Expected Available condition")
	}
	if avail.Status != metav1.ConditionFalse {
		t.Errorf("Available = %v, want False", avail.Status)
	}
	if avail.Reason != "VirtualTargetClassNotFound" {
		t.Errorf("Available reason = %q, want VirtualTargetClassNotFound", avail.Reason)
	}

	assertInt32(t, "Replicas", es.Status.Replicas, 2)
	assertInt32(t, "ReadyReplicas", es.Status.ReadyReplicas, 1)
	assertInt32(t, "UnavailableReplicas", es.Status.UnavailableReplicas, 1)

	degraded := meta.FindStatusCondition(es.Status.Conditions, "Degraded")
	if degraded == nil {
		t.Fatal("Expected Degraded condition")
	}
}

// makeExporterWithCredential returns a credentialed "exp-1" exporter
// (as if ExporterReconciler ran first).
func makeExporterWithCredential() *jumpstarterdevv1alpha1.Exporter {
	exp := makeExporter("exp-1", false, false, true)
	exp.Status.Credential = &corev1.LocalObjectReference{Name: "exp-1-exporter"}
	exp.Status.Endpoint = testEndpoint
	return exp
}

// makeCredentialSecret returns the Secret that ExporterReconciler would create
// for the given exporter name.
func makeCredentialSecret(name string) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name + "-exporter",
			Namespace: nsDefault,
		},
		Data: map[string][]byte{"token": []byte("test-token-" + name)},
	}
}

func makeCACM() *corev1.ConfigMap {
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      caConfigMapName,
			Namespace: nsDefault,
		},
		Data: map[string]string{
			caConfigMapKey: "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
		},
	}
}

func TestEnsureExporterPods_waitingWithoutCredential(t *testing.T) {
	es := makeExporterSet()
	r, _ := newReconciler(t, es, makeVTC(), makeCACM(),
		makeExporter("exp-1", false, false, true),
	)

	reconcileOnce(t, r)

	pods := listPods(t, r.Client)
	if len(pods) != 0 {
		t.Errorf("got %d pods, want 0 (credential not yet issued)", len(pods))
	}
}

func TestEnsureExporterPods_createsPodWhenCredentialReady(t *testing.T) {
	es := makeExporterSet()
	exp := makeExporterWithCredential()
	credSecret := makeCredentialSecret("exp-1")

	r, _ := newReconciler(t, es, makeVTC(), makeCACM(), exp, credSecret)

	reconcileOnce(t, r)

	var secrets corev1.SecretList
	if err := r.List(context.Background(), &secrets,
		client.InNamespace(nsDefault)); err != nil {
		t.Fatal(err)
	}
	configFound := false
	for _, s := range secrets.Items {
		if s.Name == configSecretPrefix+"exp-1" {
			configFound = true
			if _, ok := s.Data[exporterConfigKey]; !ok {
				t.Error("config Secret missing config key")
			}
		}
	}
	if !configFound {
		t.Errorf("expected config Secret %q not found", configSecretPrefix+"exp-1")
	}

	pods := listPods(t, r.Client)
	if len(pods) == 0 {
		t.Error("expected a Pod to be created, got none")
	}
}

func TestEnsureExporterPods_idempotent(t *testing.T) {
	es := makeExporterSet()
	exp := makeExporterWithCredential()
	credSecret := makeCredentialSecret("exp-1")

	r, _ := newReconciler(t, es, makeVTC(), makeCACM(), exp, credSecret)

	reconcileOnce(t, r)
	reconcileOnce(t, r)

	pods := listPods(t, r.Client)
	if len(pods) != 1 {
		t.Errorf("got %d pods after 2 reconciles, want 1 (idempotent)", len(pods))
	}
}

func TestEnsureExporterPods_configSecretHasEndpointAndToken(t *testing.T) {
	es := makeExporterSet()
	exp := makeExporterWithCredential()
	credSecret := makeCredentialSecret("exp-1")

	r, _ := newReconciler(t, es, makeVTC(), makeCACM(), exp, credSecret)
	reconcileOnce(t, r)

	var secret corev1.Secret
	if err := r.Get(context.Background(),
		types.NamespacedName{Name: configSecretPrefix + "exp-1", Namespace: nsDefault},
		&secret); err != nil {
		t.Fatalf("config Secret not found: %v", err)
	}

	configYAML, ok := secret.Data[exporterConfigKey]
	if !ok {
		t.Fatal("missing config key in config Secret")
	}

	content := string(configYAML)
	for _, want := range []string{
		"apiVersion: jumpstarter.dev/v1alpha1",
		"kind: ExporterConfig",
		"token: test-token-exp-1",
		"endpoint:",
	} {
		if !strings.Contains(content, want) {
			t.Errorf("config YAML missing %q\ngot:\n%s", want, content)
		}
	}
}

func TestEnsureExporterPods_driversAppearedInConfig(t *testing.T) {
	es := makeExporterSet(func(e *virtualtargetv1alpha1.ExporterSet) {
		e.Spec.Template.Spec.Drivers = []virtualtargetv1alpha1.DriverConfig{
			{Name: "power", Type: "jumpstarter_driver_power.driver.QemuPower"},
		}
	})
	exp := makeExporterWithCredential()
	credSecret := makeCredentialSecret("exp-1")

	r, _ := newReconciler(t, es, makeVTC(), makeCACM(), exp, credSecret)
	reconcileOnce(t, r)

	var secret corev1.Secret
	if err := r.Get(context.Background(),
		types.NamespacedName{Name: configSecretPrefix + "exp-1", Namespace: nsDefault},
		&secret); err != nil {
		t.Fatalf("config Secret not found: %v", err)
	}

	content := string(secret.Data[exporterConfigKey])
	if !strings.Contains(content, "power:") {
		t.Errorf("expected 'power:' in config YAML, got:\n%s", content)
	}
	if !strings.Contains(content, "QemuPower") {
		t.Errorf("expected 'QemuPower' in config YAML, got:\n%s", content)
	}
}

func TestEnsureExporterPods_skipsDisabledExporters(t *testing.T) {
	es := makeExporterSet()
	exp2 := makeExporter("exp-2", false, false, true)
	exp2.Status.Credential = &corev1.LocalObjectReference{Name: "exp-2-exporter"}
	exp2.Status.Endpoint = testEndpoint
	exp2.Spec.Enabled = boolPtr(false)
	credSecret := makeCredentialSecret("exp-2")

	r, _ := newReconciler(t, es, makeVTC(), makeCACM(), exp2, credSecret)
	reconcileOnce(t, r)

	pods := listPods(t, r.Client)
	if len(pods) != 0 {
		t.Errorf("got %d pods for disabled exporter, want 0", len(pods))
	}
}

func TestEnsureExporterPods_tokenRotationUpdatesConfigSecret(t *testing.T) {
	es := makeExporterSet()
	exp := makeExporterWithCredential()
	credSecret := makeCredentialSecret("exp-1")

	r, c := newReconciler(t, es, makeVTC(), makeCACM(), exp, credSecret)
	reconcileOnce(t, r)

	// Simulate token rotation.
	var latestCred corev1.Secret
	if err := c.Get(context.Background(),
		types.NamespacedName{Name: "exp-1-exporter", Namespace: nsDefault},
		&latestCred); err != nil {
		t.Fatalf("get credential Secret: %v", err)
	}
	latestCred.Data["token"] = []byte("rotated-token-xyz")
	if err := c.Update(context.Background(), &latestCred); err != nil {
		t.Fatalf("update credential Secret: %v", err)
	}

	reconcileOnce(t, r)

	var configSecret corev1.Secret
	if err := c.Get(context.Background(),
		types.NamespacedName{Name: configSecretPrefix + "exp-1", Namespace: nsDefault},
		&configSecret); err != nil {
		t.Fatalf("config Secret not found: %v", err)
	}

	content := string(configSecret.Data[exporterConfigKey])
	if !strings.Contains(content, "rotated-token-xyz") {
		t.Errorf("config Secret should contain rotated token\ngot:\n%s", content)
	}
	if strings.Contains(content, "test-token-exp-1") {
		t.Errorf("config Secret still contains stale token\ngot:\n%s", content)
	}
}

func TestBuildExportMap_duplicateKey_returnsError(t *testing.T) {
	drivers := []virtualtargetv1alpha1.DriverConfig{
		{Name: "power", Type: "jumpstarter_driver_power.driver.QemuPower"},
		{Name: "power", Type: "jumpstarter_driver_power.driver.QemuPower2"},
	}
	_, err := buildExportMap(drivers)
	if err == nil {
		t.Fatal("expected error for duplicate driver key, got nil")
	}
}

func TestMergeImages_bothNil(t *testing.T) {
	if got := mergeImages(nil, nil); got != nil {
		t.Errorf("mergeImages(nil, nil) = %v, want nil", got)
	}
}

func TestMergeImages_vtcOnly(t *testing.T) {
	vtc := &virtualtargetv1alpha1.ImageOverrides{
		Exporter: &virtualtargetv1alpha1.ImageSpec{Image: "vtc-exporter:1"},
	}
	got := mergeImages(vtc, nil)
	if got.Exporter == nil || got.Exporter.Image != "vtc-exporter:1" {
		t.Errorf("expected vtc exporter image, got %v", got)
	}
}

func TestMergeImages_esOnly(t *testing.T) {
	es := &virtualtargetv1alpha1.ImageOverrides{
		Runtime: &virtualtargetv1alpha1.ImageSpec{Image: "es-runtime:2"},
	}
	got := mergeImages(nil, es)
	if got.Runtime == nil || got.Runtime.Image != "es-runtime:2" {
		t.Errorf("expected es runtime image, got %v", got)
	}
}

func TestMergeImages_esOverridesVtc(t *testing.T) {
	vtc := &virtualtargetv1alpha1.ImageOverrides{
		Exporter: &virtualtargetv1alpha1.ImageSpec{Image: "vtc-exporter:1"},
		Runtime:  &virtualtargetv1alpha1.ImageSpec{Image: "vtc-runtime:1", ImagePullPolicy: corev1.PullAlways},
	}
	es := &virtualtargetv1alpha1.ImageOverrides{
		Runtime: &virtualtargetv1alpha1.ImageSpec{Image: "es-runtime:2"},
	}
	got := mergeImages(vtc, es)
	if got.Exporter == nil || got.Exporter.Image != "vtc-exporter:1" {
		t.Errorf("exporter should come from vtc, got %v", got.Exporter)
	}
	if got.Runtime == nil || got.Runtime.Image != "es-runtime:2" {
		t.Errorf("runtime should be overridden by es, got %v", got.Runtime)
	}
}

// --- client-visible identity labels -----------------------------------------

func TestScaleUp_stampsIdentityLabels(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 1
		es.Spec.MinAvailableReplicas = 0
	})
	r, c := newReconciler(t, es, makeVTC())
	reconcileOnce(t, r)

	exporters := listExporters(t, c)
	if len(exporters) != 1 {
		t.Fatalf("expected 1 exporter, got %d", len(exporters))
	}
	// Set membership otherwise lives only in ownerReferences, which the client
	// API never exposes.
	if got := exporters[0].Labels[labelExporterSetName]; got != "demo-set" {
		t.Errorf("%s = %q, want %q", labelExporterSetName, got, "demo-set")
	}
	if got := exporters[0].Labels[labelVirtualTargetClass]; got != "qemu-class" {
		t.Errorf("%s = %q, want %q", labelVirtualTargetClass, got, "qemu-class")
	}
	// The provisioner lives on the class, which a client cannot read.
	if got := exporters[0].Labels[labelProvisioner]; got != qemu.ProvisionerName {
		t.Errorf("%s = %q, want %q", labelProvisioner, got, qemu.ProvisionerName)
	}
	// Template labels still come through.
	if got := exporters[0].Labels["exporterset"]; got != "demo-set" {
		t.Errorf("template label lost: got %q", got)
	}
}

func TestReconcile_backfillsIdentityLabelsOnExistingExporters(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.MinReplicas = 1
		es.Spec.MinAvailableReplicas = 0
	})
	// An exporter from before these labels existed.
	existing := makeExporter("demo-set-old", true, false, true)
	delete(existing.Labels, labelExporterSetName)

	r, c := newReconciler(t, es, makeVTC(), existing)
	reconcileOnce(t, r)

	var got jumpstarterdevv1alpha1.Exporter
	if err := c.Get(context.Background(),
		types.NamespacedName{Name: "demo-set-old", Namespace: nsDefault}, &got); err != nil {
		t.Fatalf("get exporter: %v", err)
	}
	if got.Labels[labelExporterSetName] != "demo-set" {
		t.Errorf("existing exporter not labelled: %v", got.Labels)
	}
	if got.Labels[labelVirtualTargetClass] != "qemu-class" {
		t.Errorf("existing exporter missing class label: %v", got.Labels)
	}
	if got.Labels[labelProvisioner] != qemu.ProvisionerName {
		t.Errorf("existing exporter missing provisioner label: %v", got.Labels)
	}
}

func TestExporterLabels_survivesNilTemplateLabels(t *testing.T) {
	es := makeExporterSet(func(es *virtualtargetv1alpha1.ExporterSet) {
		es.Spec.Template.Metadata.Labels = nil
	})
	r, _ := newReconciler(t, es, makeVTC())
	labels := r.exporterLabels(es)
	if labels[labelExporterSetName] != "demo-set" {
		t.Errorf("expected set name label, got %v", labels)
	}
}
