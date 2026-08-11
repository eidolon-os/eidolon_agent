"""The Agent's gRPC bindings, published by the Contract Plane.

Agent owns what this contract says; ``eidolon_sdk`` is where it ships from, so
the same bindings reach Channel without a mirrored ``.proto``. Edit the
contract at ``eidolon_sdk/contracts/grpc/eidolon_agent/v1/eidolon.proto`` and
regenerate with ``eidolon_sdk/scripts/gen_grpc_stubs.sh``.

This module stays as the import surface so call sites keep using ``pb``/``pbg``.
"""

# Pre-load proto dependencies before importing the generated pb2 module.
# The grpcio-tools generator does not insert these imports for proto files
# that reference google/protobuf well-known types; without them the descriptor
# pool fails with "Depends on file '...timestamp.proto', but it has not been loaded".
from eidolon_sdk.grpc.eidolon_agent.v1 import eidolon_pb2 as pb
from eidolon_sdk.grpc.eidolon_agent.v1 import eidolon_pb2_grpc as pbg
from google.protobuf import struct_pb2 as _  # noqa: F401
from google.protobuf import timestamp_pb2 as _t  # noqa: F401

__all__ = ["pb", "pbg"]
