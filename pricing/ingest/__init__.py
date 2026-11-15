"""Telemetry ingestion context: stream events -> windowed features.

ADR-010: the logic here is the Flink job — pure, unit-tested functions that a
future Flink runner will call instead of the in-process runner. Nothing in
this package imports a broker, a framework, or a runtime.
"""
