package authorization

import (
	"context"
	"slices"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/authorization/authorizer"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type BasicAuthorizer struct {
	client       client.Client
	prefix       string
	provisioning bool
}

func NewBasicAuthorizer(client client.Client, prefix string, provisioning bool) authorizer.Authorizer {
	return &BasicAuthorizer{
		client:       client,
		prefix:       prefix,
		provisioning: provisioning,
	}
}

func (b *BasicAuthorizer) Authorize(
	ctx context.Context,
	attributes authorizer.Attributes,
) (authorizer.Decision, string, error) {
	switch attributes.GetResource() {
	case "Exporter":
		var e jumpstarterdevv1alpha1.Exporter
		if err := b.client.Get(ctx, client.ObjectKey{
			Namespace: attributes.GetNamespace(),
			Name:      attributes.GetName(),
		}, &e); err != nil {
			return authorizer.DecisionDeny, "failed to get exporter", err
		}
		if slices.Contains(e.Usernames(b.prefix), attributes.GetUser().GetName()) {
			return authorizer.DecisionAllow, "", nil
		} else {
			return authorizer.DecisionDeny, "", nil
		}
	case "Client":
		var c jumpstarterdevv1alpha1.Client
		err := b.client.Get(ctx, client.ObjectKey{
			Namespace: attributes.GetNamespace(),
			Name:      attributes.GetName(),
		}, &c)
		if err != nil {
			if apierrors.IsNotFound(err) && b.provisioning {
				c = jumpstarterdevv1alpha1.Client{
					ObjectMeta: metav1.ObjectMeta{
						Namespace: attributes.GetNamespace(),
						Name:      attributes.GetName(),
					},
					Spec: jumpstarterdevv1alpha1.ClientSpec{
						Username: new(attributes.GetUser().GetName()),
					},
				}
				if err := b.client.Create(ctx, &c); err != nil {
					return authorizer.DecisionDeny, "failed to provision client", err
				}
			} else {
				return authorizer.DecisionDeny, "failed to get client", err
			}
		}

		if slices.Contains(c.Usernames(b.prefix), attributes.GetUser().GetName()) {
			return authorizer.DecisionAllow, "", nil
		} else {
			return authorizer.DecisionDeny, "", nil
		}
	default:
		return authorizer.DecisionDeny, "invalid object kind", nil
	}
}
