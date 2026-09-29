package state

import (
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func timedOutDeployment(conditionAt time.Time) *appsv1.Deployment {
	two := int32(2)
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "inventory-service", Generation: 7},
		Spec:       appsv1.DeploymentSpec{Replicas: &two},
		Status: appsv1.DeploymentStatus{ObservedGeneration: 7, Replicas: 3, UpdatedReplicas: 1, AvailableReplicas: 1,
			Conditions: []appsv1.DeploymentCondition{{Type: appsv1.DeploymentProgressing, Status: "False",
				Reason: "ProgressDeadlineExceeded", LastUpdateTime: metav1.NewTime(conditionAt)}}},
	}
}

// Regression (found in the live benchmark): rolling back a release whose rollout had already
// timed out re-activates an existing ReplicaSet, and the Deployment keeps the old
// ProgressDeadlineExceeded condition until it observes progress. The rollback was marked
// Failed in the same second it started.
func TestStaleProgressDeadlineIgnored(t *testing.T) {
	start := time.Date(2026, 9, 29, 0, 47, 34, 0, time.UTC)
	done, failed, msg := RolloutStatus(timedOutDeployment(start.Add(-2*time.Minute)), start)
	if failed || done {
		t.Fatalf("stale deadline must mean in progress, got done=%v failed=%v (%s)", done, failed, msg)
	}
	if _, failed, _ := RolloutStatus(timedOutDeployment(start.Add(95*time.Second)), start); !failed {
		t.Fatal("a deadline exceeded during this rollout must fail it")
	}
	if _, failed, _ := RolloutStatus(timedOutDeployment(start.Add(-time.Hour)), time.Time{}); !failed {
		t.Fatal("zero 'since' must consider every condition")
	}
}
