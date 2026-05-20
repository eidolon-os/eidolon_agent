"""gRPC data plane.

Bootstrap creates a :class:`GrpcServer` with the chat + proactive servicers and
interceptors, then awaits :meth:`GrpcServer.serve` for the lifetime of the
process.
"""

from eidolon_agent.transport.grpc.server import GrpcServer

__all__ = ["GrpcServer"]
