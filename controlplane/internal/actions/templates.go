package actions

import (
	"context"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/state"
)

// RollbackTemplate resolves the pod template a rollback would restore.
func RollbackTemplate(ctx context.Context, res *state.Resolver, d *appsv1.Deployment, toRevision *int64) (*corev1.PodTemplateSpec, int64, error) {
	rss, err := res.OwnedReplicaSets(ctx, d)
	if err != nil {
		return nil, 0, err
	}
	current := state.Revision(d)
	var target *appsv1.ReplicaSet
	for i := range rss {
		rev := state.Revision(&rss[i])
		if toRevision != nil {
			if rev == *toRevision {
				target = &rss[i]
			}
		} else if rev < current && (target == nil || rev > state.Revision(target)) {
			target = &rss[i]
		}
	}
	if target == nil {
		return nil, 0, fmt.Errorf("no rollback target revision found (current %d)", current)
	}
	tmpl := target.Spec.Template.DeepCopy()
	delete(tmpl.Labels, "pod-template-hash")
	return tmpl, state.Revision(target), nil
}

// ApplyResources sets container resources on a template copy.
func ApplyResources(tmpl *corev1.PodTemplateSpec, container string, p *v1.ResourcePatch) error {
	idx := -1
	for i, c := range tmpl.Spec.Containers {
		if c.Name == container || (container == "" && len(tmpl.Spec.Containers) == 1) {
			idx = i
		}
	}
	if idx < 0 {
		return fmt.Errorf("container %q not found", container)
	}
	c := &tmpl.Spec.Containers[idx]
	if c.Resources.Limits == nil {
		c.Resources.Limits = corev1.ResourceList{}
	}
	if c.Resources.Requests == nil {
		c.Resources.Requests = corev1.ResourceList{}
	}
	setQty(c.Resources.Requests, corev1.ResourceCPU, p.CPURequest)
	setQty(c.Resources.Limits, corev1.ResourceCPU, p.CPULimit)
	setQty(c.Resources.Requests, corev1.ResourceMemory, p.MemoryRequest)
	setQty(c.Resources.Limits, corev1.ResourceMemory, p.MemoryLimit)
	return nil
}
