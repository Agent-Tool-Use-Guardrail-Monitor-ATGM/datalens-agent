"""Shared model/runner failures without provider or framework imports."""


class AdapterError(RuntimeError):
    """Fixed messages only. Do not wrap raw provider exceptions."""


class EvidenceDeliveryError(AdapterError):
    """Evidence failure prevents a response from reaching tool dispatch."""
