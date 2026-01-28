package v1alpha1

import (
	"context"
	"fmt"
	"time"

	cpb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/client/v1"
	pb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/v1"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/service/utils"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/selection"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	kclient "sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

// ReconcileLeaseTimeFields calculates missing time fields and validates consistency
// between BeginTime, EndTime, and Duration. Modifies pointers in place.
//
// Supported lease specification patterns:
// 1. Duration only (no BeginTime/EndTime): immediate start, runs for Duration
//   - BeginTime set by controller when exporter acquired
//   - EndTime = Status.BeginTime + Duration (calculated at runtime)
//
// 2. EndTime only: INVALID - cannot infer Duration without BeginTime or explicit Duration
//   - Returns error: "duration is required (must specify Duration, or both BeginTime and EndTime)"
//
// 3. BeginTime + Duration: scheduled start at BeginTime, runs for Duration
//   - Lease waits until BeginTime, then acquires exporter
//   - EndTime = BeginTime + Duration (calculated at runtime)
//
// 4. BeginTime + EndTime: scheduled window, Duration computed from times
//   - Duration = EndTime - BeginTime (auto-calculated here)
//   - Validates EndTime > BeginTime (positive duration)
//
// 5. EndTime + Duration: scheduled end, BeginTime computed as EndTime - Duration
//   - BeginTime = EndTime - Duration (auto-calculated here)
//   - Useful for "finish by" scheduling
//
// 6. BeginTime + EndTime + Duration: all three specified, validates consistency
//   - Validates Duration == EndTime - BeginTime
//   - Returns error if inconsistent: "duration conflicts with begin_time and end_time"
//
// Note: The controller never auto-populates Spec.EndTime. It calculates expiration time
// on-demand from available fields, keeping Spec.EndTime meaningful only when explicitly
// set by the user. See lease_controller.go reconcileStatusEnded for expiration logic.
func ReconcileLeaseTimeFields(beginTime, endTime **metav1.Time, duration **metav1.Duration) error {
	if *beginTime != nil && *endTime != nil {
		// Calculate duration from explicit begin/end times
		calculated := (*endTime).Sub((*beginTime).Time)
		if *duration != nil && (*duration).Duration > 0 && (*duration).Duration != calculated {
			return fmt.Errorf("duration conflicts with begin_time and end_time")
		}
		*duration = &metav1.Duration{Duration: calculated}
	} else if *endTime != nil && *duration != nil && (*duration).Duration > 0 {
		// Calculate BeginTime from EndTime - Duration (scheduled lease ending at specific time)
		*beginTime = &metav1.Time{Time: (*endTime).Add(-(*duration).Duration)}
	}

	// Validate final duration is positive (rejects nil, negative, zero)
	if *duration == nil {
		return fmt.Errorf("duration is required (must specify Duration, or both BeginTime and EndTime)")
	}
	if (*duration).Duration <= 0 {
		return fmt.Errorf("duration must be positive, got %v", (*duration).Duration)
	}
	return nil
}

// ParseLabelSelector parses a label selector string and converts it to metav1.LabelSelector.
// This function supports the != operator by first parsing with labels.Parse() which supports
// all label selector syntax including !=, then converting to metav1.LabelSelector format.
func ParseLabelSelector(selectorStr string) (*metav1.LabelSelector, error) {
	// First, try to parse using labels.Parse() which supports != operator
	parsedSelector, err := labels.Parse(selectorStr)
	if err != nil {
		return nil, fmt.Errorf("failed to parse label selector: %w", err)
	}

	// Extract requirements from the parsed selector
	requirements, selectable := parsedSelector.Requirements()
	if !selectable {
		return &metav1.LabelSelector{}, nil
	}

	// Convert requirements to metav1.LabelSelector format
	matchLabels := make(map[string]string)
	var matchExpressions []metav1.LabelSelectorRequirement

	// Track NotEquals requirements by key so we can combine them into NotIn
	notEqualsByKey := make(map[string][]string)

	for _, req := range requirements {
		key := req.Key()
		operator := req.Operator()
		values := req.ValuesUnsorted()

		switch operator {
		case selection.Equals:
			// For exact match with single value, use matchLabels
			if len(values) == 1 {
				// Check if we already have an equality requirement for this key with a different value
				if existingValue, exists := matchLabels[key]; exists && existingValue != values[0] {
					return nil, fmt.Errorf("invalid selector: label %s cannot have multiple equality requirements with different values (%s and %s)", key, existingValue, values[0])
				}
				matchLabels[key] = values[0]
			} else {
				// Multiple values should use In operator
				matchExpressions = append(matchExpressions, metav1.LabelSelectorRequirement{
					Key:      key,
					Operator: metav1.LabelSelectorOpIn,
					Values:   values,
				})
			}
		case selection.NotEquals:
			// Accumulate != requirements for the same key to combine into NotIn
			if len(values) != 1 {
				return nil, fmt.Errorf("invalid selector: != operator requires exactly one value")
			}
			notEqualsByKey[key] = append(notEqualsByKey[key], values[0])
		case selection.In:
			matchExpressions = append(matchExpressions, metav1.LabelSelectorRequirement{
				Key:      key,
				Operator: metav1.LabelSelectorOpIn,
				Values:   values,
			})
		case selection.NotIn:
			matchExpressions = append(matchExpressions, metav1.LabelSelectorRequirement{
				Key:      key,
				Operator: metav1.LabelSelectorOpNotIn,
				Values:   values,
			})
		case selection.Exists:
			matchExpressions = append(matchExpressions, metav1.LabelSelectorRequirement{
				Key:      key,
				Operator: metav1.LabelSelectorOpExists,
				Values:   []string{},
			})
		case selection.DoesNotExist:
			matchExpressions = append(matchExpressions, metav1.LabelSelectorRequirement{
				Key:      key,
				Operator: metav1.LabelSelectorOpDoesNotExist,
				Values:   []string{},
			})
		default:
			return nil, fmt.Errorf("unsupported label selector operator: %v", operator)
		}
	}

	// Convert accumulated NotEquals requirements to NotIn expressions
	for key, vals := range notEqualsByKey {
		matchExpressions = append(matchExpressions, metav1.LabelSelectorRequirement{
			Key:      key,
			Operator: metav1.LabelSelectorOpNotIn,
			Values:   vals,
		})
	}

	return &metav1.LabelSelector{
		MatchLabels:      matchLabels,
		MatchExpressions: matchExpressions,
	}, nil
}

func LeaseFromProtobuf(
	req *cpb.Lease,
	key types.NamespacedName,
	clientRef corev1.LocalObjectReference,
) (*Lease, error) {
	selector, err := ParseLabelSelector(req.Selector)
	if err != nil {
		return nil, err
	}

	var beginTime, endTime *metav1.Time
	var duration *metav1.Duration

	if req.BeginTime != nil {
		beginTime = &metav1.Time{Time: req.BeginTime.AsTime()}
	}
	if req.EndTime != nil {
		endTime = &metav1.Time{Time: req.EndTime.AsTime()}
	}
	if req.Duration != nil {
		duration = &metav1.Duration{Duration: req.Duration.AsDuration()}
	}
	if err := ReconcileLeaseTimeFields(&beginTime, &endTime, &duration); err != nil {
		return nil, err
	}

	return &Lease{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: key.Namespace,
			Name:      key.Name,
			Labels:    selector.MatchLabels,
		},
		Spec: LeaseSpec{
			ClientRef: clientRef,
			Duration:  duration,
			Selector:  *selector,
			BeginTime: beginTime,
			EndTime:   endTime,
		},
	}, nil
}

func (l *Lease) ToProtobuf() *cpb.Lease {
	var conditions []*pb.Condition
	for _, condition := range l.Status.Conditions {
		conditions = append(conditions, &pb.Condition{
			Type:               &condition.Type,
			Status:             (*string)(&condition.Status),
			ObservedGeneration: &condition.ObservedGeneration,
			LastTransitionTime: &pb.Time{
				Seconds: &condition.LastTransitionTime.ProtoTime().Seconds,
				Nanos:   &condition.LastTransitionTime.ProtoTime().Nanos,
			},
			Reason:  &condition.Reason,
			Message: &condition.Message,
		})
	}

	lease := cpb.Lease{
		Name:       fmt.Sprintf("namespaces/%s/leases/%s", l.Namespace, l.Name),
		Selector:   metav1.FormatLabelSelector(&l.Spec.Selector),
		Client:     ptr.To(fmt.Sprintf("namespaces/%s/clients/%s", l.Namespace, l.Spec.ClientRef.Name)),
		Conditions: conditions,
	}
	if l.Spec.Duration != nil {
		lease.Duration = durationpb.New(l.Spec.Duration.Duration)
	}

	// Requested/planned times from Spec
	if l.Spec.BeginTime != nil {
		lease.BeginTime = timestamppb.New(l.Spec.BeginTime.Time)
	}
	if l.Spec.EndTime != nil {
		lease.EndTime = timestamppb.New(l.Spec.EndTime.Time)
	}

	// Actual times from Status
	if l.Status.BeginTime != nil {
		lease.EffectiveBeginTime = timestamppb.New(l.Status.BeginTime.Time)
		endTime := time.Now()
		if l.Status.EndTime != nil {
			endTime = l.Status.EndTime.Time
			lease.EffectiveEndTime = timestamppb.New(endTime)
		}
		// Final effective duration or current one so far while active. Non-negative to handle clock skew.
		effectiveDuration := max(endTime.Sub(l.Status.BeginTime.Time), 0)
		lease.EffectiveDuration = durationpb.New(effectiveDuration)
	}
	if l.Status.ExporterRef != nil {
		lease.Exporter = ptr.To(utils.UnparseExporterIdentifier(kclient.ObjectKey{
			Namespace: l.Namespace,
			Name:      l.Status.ExporterRef.Name,
		}))
	}

	return &lease
}

func (l *LeaseList) ToProtobuf() *cpb.ListLeasesResponse {
	var jleases []*cpb.Lease
	for _, jlease := range l.Items {
		jleases = append(jleases, jlease.ToProtobuf())
	}
	return &cpb.ListLeasesResponse{
		Leases:        jleases,
		NextPageToken: l.Continue,
	}
}

func (l *Lease) GetExporterSelector() (labels.Selector, error) {
	return metav1.LabelSelectorAsSelector(&l.Spec.Selector)
}

func (l *Lease) SetStatusPending(reason, messageFormat string, a ...any) {
	l.SetStatusCondition(LeaseConditionTypePending, true, reason, messageFormat, a...)
}

func (l *Lease) SetStatusReady(status bool, reason, messageFormat string, a ...any) {
	l.SetStatusCondition(LeaseConditionTypeReady, status, reason, messageFormat, a...)
}

func (l *Lease) SetStatusUnsatisfiable(reason, messageFormat string, a ...any) {
	l.SetStatusCondition(LeaseConditionTypeUnsatisfiable, true, reason, messageFormat, a...)
}

func (l *Lease) SetStatusInvalid(reason, messageFormat string, a ...any) {
	l.SetStatusCondition(LeaseConditionTypeInvalid, true, reason, messageFormat, a...)
}

func (l *Lease) SetStatusCondition(
	condition LeaseConditionType,
	status bool,
	reason, messageFormat string, a ...any) {

	var statusCondition metav1.ConditionStatus

	if status {
		statusCondition = metav1.ConditionTrue
	} else {
		statusCondition = metav1.ConditionFalse
	}

	meta.SetStatusCondition(&l.Status.Conditions, metav1.Condition{
		Type:               string(condition),
		Status:             statusCondition,
		ObservedGeneration: l.Generation,
		LastTransitionTime: metav1.Time{
			Time: time.Now(),
		},
		Reason:  reason,
		Message: fmt.Sprintf(messageFormat, a...),
	})
}

func (l *Lease) GetExporterName() string {
	if l.Status.ExporterRef == nil {
		return "(none)"
	}
	return l.Status.ExporterRef.Name
}

func (l *Lease) GetClientName() string {
	return l.Spec.ClientRef.Name
}

func (l *Lease) Release(ctx context.Context) {
	logger := log.FromContext(ctx)
	logger.Info("The lease has been marked for release", "lease", l.Name, "exporter", l.GetExporterName(), "client", l.GetClientName())
	l.SetStatusReady(false, "Released", "The lease was marked for release")
	l.Status.Ended = true
	l.Status.EndTime = &metav1.Time{Time: time.Now()}
}

func (l *Lease) Expire(ctx context.Context) {
	logger := log.FromContext(ctx)
	logger.Info("The lease has expired", "lease", l.Name, "exporter", l.GetExporterName(), "client", l.GetClientName())
	l.SetStatusReady(false, "Expired", "The lease has expired")
	l.Status.Ended = true
	l.Status.EndTime = &metav1.Time{Time: time.Now()}
}
