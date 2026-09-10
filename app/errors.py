class ApiNotFoundError(LookupError):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ApiUnauthorizedError(RuntimeError):
    """No usable credentials. Rendered as 401.

    Missing, malformed, expired and wrongly-signed tokens all raise this with
    the same message: distinguishing them in the response tells a caller how
    close their guess came.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ApiForbiddenError(RuntimeError):
    """Authenticated, but not allowed. Rendered as 403.

    Only for role checks. A job belonging to someone else is a 404, never this
    -- a 403 confirms the id exists and lets someone probe for valid ids.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
