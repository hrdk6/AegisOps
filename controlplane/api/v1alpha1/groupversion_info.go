// Package v1alpha1 contains the AegisOps control-plane API (group aegisops.io).
//
// The CRDs in this package are the contract between the (untrusted) AI layer and
// the (trusted) deterministic control plane: the AI can only *request* work by
// having the controller create these objects; the controller alone decides and
// executes.
//
// +kubebuilder:object:generate=true
// +groupName=aegisops.io
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	// GroupVersion is group version used to register these objects.
	GroupVersion = schema.GroupVersion{Group: "aegisops.io", Version: "v1alpha1"}

	// SchemeBuilder is used to add go types to the GroupVersionKind scheme.
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

	// AddToScheme adds the types in this group-version to the given scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
