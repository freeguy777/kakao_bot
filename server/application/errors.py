from __future__ import annotations


class FeatureExecutionError(RuntimeError):
    def __init__(self, feature_key: str, detail: str) -> None:
        super().__init__(detail)
        self.feature_key = feature_key
        self.detail = detail

