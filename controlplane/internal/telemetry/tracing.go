package telemetry

import (
	"context"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

// Tracer is the controller's tracer.
var Tracer trace.Tracer = otel.Tracer("aegisops-controller")

// InitTracing configures OTLP/gRPC export when OTEL_EXPORTER_OTLP_ENDPOINT is
// set. It returns a shutdown function; without an endpoint tracing is a no-op.
func InitTracing(ctx context.Context, service string) (func(context.Context) error, error) {
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		return func(context.Context) error { return nil }, nil
	}
	exp, err := otlptracegrpc.New(ctx, otlptracegrpc.WithInsecure())
	if err != nil {
		return nil, err
	}
	// Schemaless attributes merge cleanly with the SDK default resource regardless of its schema version.
	res, err := resource.Merge(resource.Default(), resource.NewSchemaless(
		semconv.ServiceName(service), semconv.ServiceNamespace("aegisops")))
	if err != nil {
		return nil, err
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp, sdktrace.WithBatchTimeout(2*time.Second)), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)
	Tracer = tp.Tracer("aegisops-controller")
	return tp.Shutdown, nil
}
