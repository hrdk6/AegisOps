"""Failure modes are driven by ordinary configuration, and the baseline configuration is healthy."""

from __future__ import annotations

import time

import pytest

from shopflow import behaviors


def test_fraud_model_cost_depends_on_release_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAUD_MODEL", "gbdt-v1")
    assert behaviors.fraud_model_cost_ms() == 15.0
    monkeypatch.setenv("FRAUD_MODEL", "gbdt-v2")
    assert behaviors.fraud_model_cost_ms() == 90.0


def test_cpu_work_burns_roughly_the_requested_cpu_time() -> None:
    t0 = time.process_time()
    behaviors.cpu_work(20)
    assert 0.018 <= time.process_time() - t0 < 0.2


def test_baseline_rounding_mode_never_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYMENT_ROUNDING_MODE", raising=False)
    assert not any(behaviors.rounding_mismatch() for _ in range(500))


def test_faulty_rounding_mode_fails_a_fraction_of_charges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENT_ROUNDING_MODE", "bankers-v2")
    rate = sum(behaviors.rounding_mismatch() for _ in range(4000)) / 4000
    assert 0.25 < rate < 0.45
