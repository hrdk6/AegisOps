// Command controller runs the AegisOps control plane: the RemediationAction,
// RemediationSimulation and CanaryRelease reconcilers, the change recorder and
// the authenticated control-plane API.
package main

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	toolscache "k8s.io/client-go/tools/cache"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	v1 "github.com/aegisops/aegisops/controlplane/api/v1alpha1"
	"github.com/aegisops/aegisops/controlplane/internal/actions"
	"github.com/aegisops/aegisops/controlplane/internal/approval"
	"github.com/aegisops/aegisops/controlplane/internal/audit"
	"github.com/aegisops/aegisops/controlplane/internal/changelog"
	"github.com/aegisops/aegisops/controlplane/internal/controllers"
	"github.com/aegisops/aegisops/controlplane/internal/history"
	"github.com/aegisops/aegisops/controlplane/internal/httpapi"
	"github.com/aegisops/aegisops/controlplane/internal/notify"
	"github.com/aegisops/aegisops/controlplane/internal/promclient"
	"github.com/aegisops/aegisops/controlplane/internal/sandbox"
	"github.com/aegisops/aegisops/controlplane/internal/state"
	"github.com/aegisops/aegisops/controlplane/internal/telemetry"
)

// leaderless runs on every replica (API serving, cache notifications).
type leaderless func(context.Context) error

func (l leaderless) Start(ctx context.Context) error { return l(ctx) }
func (l leaderless) NeedLeaderElection() bool        { return false }

// leaderOnly runs only on the elected leader (writes).
type leaderOnly func(context.Context) error

func (l leaderOnly) Start(ctx context.Context) error { return l(ctx) }
func (l leaderOnly) NeedLeaderElection() bool        { return true }

func split(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func nsConfig(names ...string) map[string]cache.Config {
	m := map[string]cache.Config{}
	for _, n := range names {
		m[n] = cache.Config{}
	}
	return m
}

func main() {
	var (
		metricsAddr, probeAddr, apiAddr         string
		leaderElect                             bool
		watchNamespaces                         string
		systemNamespace, sandboxNamespace       string
		policyName, promURL, approvalKeyFile    string
		audience, bindings                      string
		probeImage, probeCommand                string
		sandboxPostgresImage, sandboxRedisImage string
	)
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "Prometheus metrics address")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "Health probe address")
	flag.StringVar(&apiAddr, "api-bind-address", ":8082", "Control-plane API address")
	flag.BoolVar(&leaderElect, "leader-elect", true, "Enable leader election")
	flag.StringVar(&watchNamespaces, "watch-namespaces", "shop", "Comma-separated namespaces under management")
	flag.StringVar(&systemNamespace, "system-namespace", "aegis-system", "Namespace holding AegisOps CRs")
	flag.StringVar(&sandboxNamespace, "sandbox-namespace", "aegis-sandbox", "Namespace for isolated simulations")
	flag.StringVar(&policyName, "policy-name", "default", "Name of the cluster AegisPolicy")
	flag.StringVar(&promURL, "prometheus-url", "http://prometheus.observability.svc:9090", "Prometheus base URL")
	flag.StringVar(&approvalKeyFile, "approval-key-file", "/etc/aegis/approval/key", "HMAC key for approval signatures")
	flag.StringVar(&audience, "token-audience", "aegisops-controlplane", "Audience required on caller tokens")
	flag.StringVar(&bindings, "role-bindings", "", "subject=role,role;... mapping of ServiceAccounts to API roles")
	flag.StringVar(&probeImage, "probe-image", "aegisops/shopflow:dev", "Image running the simulation load probe")
	flag.StringVar(&probeCommand, "probe-command", "python,-m,shopflow.loadprobe", "Comma-separated probe command")
	flag.StringVar(&sandboxPostgresImage, "sandbox-postgres-image", "postgres:16-alpine", "Postgres sidecar image for simulations")
	flag.StringVar(&sandboxRedisImage, "sandbox-redis-image", "redis:7.4-alpine", "Redis sidecar image for simulations")
	opts := zap.Options{Development: false}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	log := ctrl.Log.WithName("setup")

	if err := run(runConfig{metricsAddr, probeAddr, apiAddr, leaderElect, split(watchNamespaces), systemNamespace,
		sandboxNamespace, policyName, promURL, approvalKeyFile, audience, bindings, probeImage, split(probeCommand),
		sandboxPostgresImage, sandboxRedisImage}); err != nil {
		log.Error(err, "controller exited with error")
		os.Exit(1)
	}
}

type runConfig struct {
	metricsAddr, probeAddr, apiAddr         string
	leaderElect                             bool
	watch                                   []string
	systemNamespace, sandboxNamespace       string
	policyName, promURL, approvalKeyFile    string
	audience, bindings                      string
	probeImage                              string
	probeCommand                            []string
	sandboxPostgresImage, sandboxRedisImage string
}

func run(c runConfig) error {
	log := ctrl.Log.WithName("setup")
	ctx := ctrl.SetupSignalHandler()

	shutdown, err := telemetry.InitTracing(ctx, "aegisops-controller")
	if err != nil {
		return fmt.Errorf("init tracing: %w", err)
	}
	defer func() { _ = shutdown(context.Background()) }()

	key, err := os.ReadFile(c.approvalKeyFile)
	if err != nil {
		return fmt.Errorf("read approval key: %w", err)
	}
	verifier, err := approval.NewVerifier([]byte(strings.TrimSpace(string(key))))
	if err != nil {
		return err
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := v1.AddToScheme(scheme); err != nil {
		return err
	}

	all := append(append([]string{}, c.watch...), c.systemNamespace, c.sandboxNamespace)
	watchAndSandbox := append(append([]string{}, c.watch...), c.sandboxNamespace)
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                  scheme,
		Metrics:                 metricsserver.Options{BindAddress: c.metricsAddr},
		HealthProbeBindAddress:  c.probeAddr,
		LeaderElection:          c.leaderElect,
		LeaderElectionID:        "aegisops-controller.aegisops.io",
		LeaderElectionNamespace: c.systemNamespace,
		Cache: cache.Options{
			DefaultNamespaces: nsConfig(all...),
			ByObject: map[client.Object]cache.ByObject{
				&appsv1.Deployment{}:          {Namespaces: nsConfig(watchAndSandbox...)},
				&appsv1.ReplicaSet{}:          {Namespaces: nsConfig(c.watch...)},
				&corev1.Pod{}:                 {Namespaces: nsConfig(watchAndSandbox...)},
				&corev1.Event{}:               {Namespaces: nsConfig(c.watch...)},
				&corev1.Service{}:             {Namespaces: nsConfig(c.sandboxNamespace)},
				&batchv1.Job{}:                {Namespaces: nsConfig(c.sandboxNamespace)},
				&networkingv1.NetworkPolicy{}: {Namespaces: nsConfig(c.watch...)},
				&v1.RemediationAction{}:       {Namespaces: nsConfig(c.systemNamespace)},
				&v1.RemediationSimulation{}:   {Namespaces: nsConfig(c.systemNamespace)},
				&v1.CanaryRelease{}:           {Namespaces: nsConfig(c.watch...)},
			},
		},
	})
	if err != nil {
		return fmt.Errorf("create manager: %w", err)
	}

	hist := history.NewStore(mgr.GetClient(), c.systemNamespace, 10)
	resolver := &state.Resolver{Client: mgr.GetClient(), History: hist}
	recorder := changelog.NewRecorder(c.watch, hist, 2000, ctrl.Log)
	auditLog := audit.New(ctrl.Log, 5000)
	bus := notify.New()
	provider := &controllers.PolicyProvider{Client: mgr.GetClient(), Name: c.policyName}
	events := mgr.GetEventRecorderFor("aegisops-controller")

	if err := (&controllers.ActionReconciler{
		Client: mgr.GetClient(), Recorder: events, Policy: provider, Resolver: resolver, Audit: auditLog,
		Executor: &actions.Executor{Client: mgr.GetClient(), Resolver: resolver, History: hist},
	}).SetupWithManager(mgr, c.systemNamespace); err != nil {
		return fmt.Errorf("setup action controller: %w", err)
	}
	if err := (&controllers.SimulationReconciler{
		Client: mgr.GetClient(), Recorder: events, Policy: provider, Resolver: resolver, History: hist, Audit: auditLog,
		Sandbox: sandbox.Config{Namespace: c.sandboxNamespace, PostgresImage: c.sandboxPostgresImage, RedisImage: c.sandboxRedisImage,
			ProbeImage: c.probeImage, ProbeCommand: c.probeCommand, ServiceAccount: "aegis-sandbox-runner"},
	}).SetupWithManager(mgr); err != nil {
		return fmt.Errorf("setup simulation controller: %w", err)
	}
	if err := (&controllers.CanaryReconciler{
		Client: mgr.GetClient(), Recorder: events, Prom: promclient.New(c.promURL), Queries: controllers.DefaultCanaryQueries, Audit: auditLog,
	}).SetupWithManager(mgr); err != nil {
		return fmt.Errorf("setup canary controller: %w", err)
	}
	if err := mgr.Add(&controllers.PolicyStatusReporter{Client: mgr.GetClient(), PolicyName: c.policyName,
		SystemNamespace: c.systemNamespace, Interval: 15 * time.Second}); err != nil {
		return err
	}

	// Change recording writes ConfigMap history: leader only.
	if err := mgr.Add(leaderOnly(func(ctx context.Context) error {
		if err := recorder.Register(ctx, mgr.GetCache()); err != nil {
			return err
		}
		<-ctx.Done()
		return nil
	})); err != nil {
		return err
	}
	// Informer-driven notifications for API long-polls: every replica.
	if err := mgr.Add(leaderless(func(ctx context.Context) error {
		for prefix, obj := range map[string]client.Object{"action/": &v1.RemediationAction{}, "simulation/": &v1.RemediationSimulation{}} {
			inf, err := mgr.GetCache().GetInformer(ctx, obj)
			if err != nil {
				return err
			}
			p := prefix
			notifyFn := func(o any) {
				if co, ok := o.(client.Object); ok {
					bus.Notify(p + co.GetName())
				}
			}
			if _, err := inf.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
				AddFunc: notifyFn, UpdateFunc: func(_, n any) { notifyFn(n) },
			}); err != nil {
				return err
			}
		}
		<-ctx.Done()
		return nil
	})); err != nil {
		return err
	}

	binds, err := httpapi.ParseBindings(c.bindings)
	if err != nil {
		return fmt.Errorf("parse role bindings: %w", err)
	}
	auth := httpapi.ChainAuthenticator{&httpapi.TokenReviewAuthenticator{Client: mgr.GetClient(), Audience: c.audience,
		Bindings: binds, TTL: time.Minute}}
	if spec := os.Getenv("AEGIS_CP_STATIC_TOKENS"); spec != "" {
		static, err := httpapi.NewStaticTokenAuthenticator(spec)
		if err != nil {
			return fmt.Errorf("static tokens: %w", err)
		}
		log.Info("WARNING: static development tokens enabled for the control-plane API")
		auth = append(auth, static)
	}
	if err := mgr.Add(&httpapi.Server{
		Addr: c.apiAddr, Client: mgr.GetClient(), Auth: auth, Verifier: verifier,
		Evaluator: &controllers.Evaluator{Client: mgr.GetClient(), Policy: provider, Resolver: resolver, SystemNamespace: c.systemNamespace},
		Resolver:  resolver, Changes: recorder, Audit: auditLog, Notify: bus, PolicyName: c.policyName,
		SystemNamespace: c.systemNamespace, Namespaces: c.watch, Log: ctrl.Log.WithName("api"),
	}); err != nil {
		return err
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		return err
	}
	if err := mgr.AddReadyzCheck("readyz", func(req *http.Request) error {
		if !mgr.GetCache().WaitForCacheSync(req.Context()) {
			return fmt.Errorf("cache not synced")
		}
		return nil
	}); err != nil {
		return err
	}
	log.Info("starting AegisOps controller", "watch", c.watch, "system", c.systemNamespace, "sandbox", c.sandboxNamespace)
	return mgr.Start(ctx)
}
