from graphene_django.views import GraphQLView
from graphql import ExecutionResult, GraphQLError

_VERIFICATION_MARKER = "EmailVerification"
_GENERIC_MESSAGE = "The verification request could not be processed."


class SanitizedGraphQLView(GraphQLView):
    """Hides framework error text for documents touching email verification.

    Variable-coercion, syntax and unexpected resolver errors can echo submitted
    values, so every error of such a document is replaced by one generic message.
    """

    def execute_graphql_request(self, request, data, query, variables, operation_name,
                                show_graphiql=False):
        result = super().execute_graphql_request(
            request, data, query, variables, operation_name, show_graphiql
        )
        if (
            result is None
            or not result.errors
            or not isinstance(query, str)
            or _VERIFICATION_MARKER not in query
        ):
            return result
        errors = [GraphQLError(_GENERIC_MESSAGE, path=error.path) for error in result.errors]
        return ExecutionResult(data=result.data, errors=errors)
