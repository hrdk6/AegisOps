// Package history durably records ConfigMap revisions so that configuration
// changes can be restored (update_config) even after a controller restart.
// Kubernetes itself keeps no history for ConfigMaps.
package history

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const dataKey = "versions.json"

// Version is one recorded ConfigMap revision.
type Version struct {
	Revision        string            `json:"revision"`
	RecordedAt      time.Time         `json:"recordedAt"`
	ResourceVersion string            `json:"resourceVersion"`
	Data            map[string]string `json:"data"`
}

// Store persists versions into ConfigMaps in the system namespace.
type Store struct {
	client    client.Client
	namespace string
	max       int
	mu        sync.Mutex
	cache     map[string][]Version
}

// NewStore returns a store writing into namespace.
func NewStore(c client.Client, namespace string, max int) *Store {
	return &Store{client: c, namespace: namespace, max: max, cache: map[string][]Version{}}
}

// DataHash is a short content hash of ConfigMap data.
func DataHash(data map[string]string) string {
	keys := make([]string, 0, len(data))
	for k := range data {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	for _, k := range keys {
		fmt.Fprintf(h, "%s=%s\x00", k, data[k])
	}
	return hex.EncodeToString(h.Sum(nil))[:12]
}

func key(ns, name string) string { return ns + "/" + name }

func storeName(ns, name string) string {
	n := "aegis-cmhist-" + ns + "-" + name
	if len(n) > 250 {
		sum := sha256.Sum256([]byte(n))
		n = "aegis-cmhist-" + hex.EncodeToString(sum[:])[:32]
	}
	return strings.ToLower(n)
}

func (s *Store) load(ctx context.Context, ns, name string) ([]Version, error) {
	if v, ok := s.cache[key(ns, name)]; ok {
		return v, nil
	}
	var cm corev1.ConfigMap
	err := s.client.Get(ctx, client.ObjectKey{Namespace: s.namespace, Name: storeName(ns, name)}, &cm)
	if apierrors.IsNotFound(err) {
		s.cache[key(ns, name)] = nil
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var versions []Version
	if raw := cm.Data[dataKey]; raw != "" {
		if err := json.Unmarshal([]byte(raw), &versions); err != nil {
			return nil, fmt.Errorf("decode history for %s/%s: %w", ns, name, err)
		}
	}
	s.cache[key(ns, name)] = versions
	return versions, nil
}

func (s *Store) persist(ctx context.Context, ns, name string, versions []Version) error {
	raw, err := json.Marshal(versions)
	if err != nil {
		return err
	}
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: s.namespace, Name: storeName(ns, name),
		Labels:      map[string]string{"app.kubernetes.io/managed-by": "aegisops-controller", "aegisops.io/config-history": "true"},
		Annotations: map[string]string{"aegisops.io/source": key(ns, name)}}}
	var existing corev1.ConfigMap
	err = s.client.Get(ctx, client.ObjectKeyFromObject(cm), &existing)
	switch {
	case apierrors.IsNotFound(err):
		cm.Data = map[string]string{dataKey: string(raw)}
		return s.client.Create(ctx, cm)
	case err != nil:
		return err
	default:
		existing.Data = map[string]string{dataKey: string(raw)}
		return s.client.Update(ctx, &existing)
	}
}

// Record appends the ConfigMap's current data if it differs from the latest version.
func (s *Store) Record(ctx context.Context, cm *corev1.ConfigMap) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	versions, err := s.load(ctx, cm.Namespace, cm.Name)
	if err != nil {
		return err
	}
	rev := DataHash(cm.Data)
	if n := len(versions); n > 0 && versions[n-1].Revision == rev {
		return nil
	}
	data := map[string]string{}
	for k, v := range cm.Data {
		data[k] = v
	}
	versions = append(versions, Version{Revision: rev, RecordedAt: time.Now().UTC(), ResourceVersion: cm.ResourceVersion, Data: data})
	if len(versions) > s.max {
		versions = versions[len(versions)-s.max:]
	}
	if err := s.persist(ctx, cm.Namespace, cm.Name, versions); err != nil {
		return err
	}
	s.cache[key(cm.Namespace, cm.Name)] = versions
	return nil
}

// Versions returns recorded versions, oldest first.
func (s *Store) Versions(ctx context.Context, ns, name string) ([]Version, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, err := s.load(ctx, ns, name)
	return append([]Version(nil), v...), err
}

// Resolve returns the requested revision. "previous" means the version recorded
// immediately before the most recent one.
func (s *Store) Resolve(ctx context.Context, ns, name, revision string) (*Version, error) {
	versions, err := s.Versions(ctx, ns, name)
	if err != nil {
		return nil, err
	}
	if revision == "" || revision == "previous" {
		if len(versions) < 2 {
			return nil, fmt.Errorf("no previous revision recorded for %s/%s", ns, name)
		}
		v := versions[len(versions)-2]
		return &v, nil
	}
	for i := len(versions) - 1; i >= 0; i-- {
		if versions[i].Revision == revision {
			v := versions[i]
			return &v, nil
		}
	}
	return nil, fmt.Errorf("revision %q not recorded for %s/%s", revision, ns, name)
}

// HasRevision implements state.ConfigHistory.
func (s *Store) HasRevision(ctx context.Context, ns, name, revision string) bool {
	_, err := s.Resolve(ctx, ns, name, revision)
	return err == nil
}
