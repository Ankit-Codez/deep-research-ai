"""Application control flow that agent provider-error handlers must propagate."""


class ResearchPaused(Exception):
    """Execution stopped safely; restarting a provider request is not implicit."""
    def __init__(self,message,code='provider_outcome_unknown'):
        self.code = code
        super().__init__(message)


class CachedProviderFailure(Exception):
    def __init__(self, status=None):
        self.status_code = status
        super().__init__('A saved provider attempt failed.')
