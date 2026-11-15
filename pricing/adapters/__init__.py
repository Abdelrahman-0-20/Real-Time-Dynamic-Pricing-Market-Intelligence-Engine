"""Adapters (ADR-002): ports implemented over real infrastructure.

This is the ONLY layer allowed to import vendor/IO libraries (Redis, Kafka,
Feast, ONNX, HTTP clients). The core must never import this package.
"""
