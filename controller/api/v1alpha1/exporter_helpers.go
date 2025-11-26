package v1alpha1

import (
	"strings"

	cpb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/client/v1"
	pb "github.com/jumpstarter-dev/jumpstarter-controller/internal/protocol/jumpstarter/v1"
	"github.com/jumpstarter-dev/jumpstarter-controller/internal/service/utils"
	"k8s.io/apimachinery/pkg/api/meta"
	kclient "sigs.k8s.io/controller-runtime/pkg/client"
)

func (e *Exporter) InternalSubject() string {
	namespace, uid := getNamespaceAndUID(e.Namespace, e.UID, e.Annotations)
	return strings.Join([]string{"exporter", namespace, e.Name, uid}, ":")
}

func (e *Exporter) Usernames(prefix string) []string {
	usernames := []string{prefix + e.InternalSubject()}

	if e.Spec.Username != nil {
		usernames = append(usernames, *e.Spec.Username)
	}

	return usernames
}

func (e *Exporter) ToProtobuf() *cpb.Exporter {
	// get online status from conditions (deprecated, kept for backward compatibility)
	isOnline := meta.IsStatusConditionTrue(e.Status.Conditions, string(ExporterConditionTypeOnline))

	return &cpb.Exporter{
		Name:          utils.UnparseExporterIdentifier(kclient.ObjectKeyFromObject(e)),
		Labels:        e.Labels,
		Online:        isOnline,
		Status:        stringToProtoStatus(e.Status.ExporterStatusValue),
		StatusMessage: e.Status.StatusMessage,
	}
}

// stringToProtoStatus converts the CRD string value to the proto ExporterStatus enum
func stringToProtoStatus(state string) pb.ExporterStatus {
	switch state {
	case ExporterStatusOffline:
		return pb.ExporterStatus_EXPORTER_STATUS_OFFLINE
	case ExporterStatusAvailable:
		return pb.ExporterStatus_EXPORTER_STATUS_AVAILABLE
	case ExporterStatusBeforeLeaseHook:
		return pb.ExporterStatus_EXPORTER_STATUS_BEFORE_LEASE_HOOK
	case ExporterStatusLeaseReady:
		return pb.ExporterStatus_EXPORTER_STATUS_LEASE_READY
	case ExporterStatusAfterLeaseHook:
		return pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK
	case ExporterStatusBeforeLeaseHookFailed:
		return pb.ExporterStatus_EXPORTER_STATUS_BEFORE_LEASE_HOOK_FAILED
	case ExporterStatusAfterLeaseHookFailed:
		return pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK_FAILED
	default:
		return pb.ExporterStatus_EXPORTER_STATUS_UNSPECIFIED
	}
}

func (l *ExporterList) ToProtobuf() *cpb.ListExportersResponse {
	var jexporters []*cpb.Exporter
	for _, jexporter := range l.Items {
		jexporters = append(jexporters, jexporter.ToProtobuf())
	}
	return &cpb.ListExportersResponse{
		Exporters:     jexporters,
		NextPageToken: l.Continue,
	}
}
