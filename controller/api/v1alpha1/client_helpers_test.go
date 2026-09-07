package v1alpha1

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func TestClient_InternalSubject(t *testing.T) {
	t.Run("without annotations", func(t *testing.T) {
		c := &Client{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "my-client",
				Namespace: "default",
				UID:       types.UID("123e4567-e89b-12d3-a456-426614174000"),
			},
		}
		expected := "client:default:my-client:123e4567-e89b-12d3-a456-426614174000"
		if got := c.InternalSubject(); got != expected {
			t.Errorf("got %v, want %v", got, expected)
		}
	})

	t.Run("with both migrated annotations", func(t *testing.T) {
		c := &Client{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "my-client",
				Namespace: "default",
				UID:       types.UID("123e4567-e89b-12d3-a456-426614174000"),
				Annotations: map[string]string{
					AnnotationMigratedNamespace: "old-namespace",
					AnnotationMigratedUID:       "old-uid-value",
				},
			},
		}
		expected := "client:old-namespace:my-client:old-uid-value"
		if got := c.InternalSubject(); got != expected {
			t.Errorf("got %v, want %v", got, expected)
		}
	})

	t.Run("empty annotation values are ignored", func(t *testing.T) {
		c := &Client{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "my-client",
				Namespace: "default",
				UID:       types.UID("123e4567-e89b-12d3-a456-426614174000"),
				Annotations: map[string]string{
					AnnotationMigratedNamespace: "",
					AnnotationMigratedUID:       "",
				},
			},
		}
		expected := "client:default:my-client:123e4567-e89b-12d3-a456-426614174000"
		if got := c.InternalSubject(); got != expected {
			t.Errorf("got %v, want %v", got, expected)
		}
	})
}

func TestClient_Usernames(t *testing.T) {
	t.Run("without custom username", func(t *testing.T) {
		c := &Client{
			ObjectMeta: metav1.ObjectMeta{Name: "my-client", Namespace: "default", UID: types.UID("123")},
			Spec:       ClientSpec{},
		}
		got := c.Usernames("internal:")
		if len(got) != 1 || got[0] != "internal:client:default:my-client:123" {
			t.Errorf("got %v, want single internal subject", got)
		}
	})

	t.Run("with custom username", func(t *testing.T) {
		c := &Client{
			ObjectMeta: metav1.ObjectMeta{Name: "my-client", Namespace: "default", UID: types.UID("123")},
			Spec:       ClientSpec{Username: new("custom-user")},
		}
		got := c.Usernames("internal:")
		if len(got) != 2 || got[1] != "custom-user" {
			t.Errorf("got %v, want internal subject and custom username", got)
		}
	})
}
