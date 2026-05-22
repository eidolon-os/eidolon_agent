"""Generated gRPC stubs for eidolon.proto.

Generated files are git-ignored; rebuild with::

    python -m grpc_tools.protoc -I eidolon_agent/app/transport/grpc/proto \
        --python_out=eidolon_agent/app/transport/grpc/proto \
        --grpc_python_out=eidolon_agent/app/transport/grpc/proto \
        --pyi_out=eidolon_agent/app/transport/grpc/proto \
        eidolon_agent/app/transport/grpc/proto/eidolon.proto

The bootstrap script (``deploy/dev/init.sh``) runs the build automatically.
"""

# Pre-load proto dependencies before importing the generated pb2 module.
# The grpcio-tools generator does not insert these imports for proto files
# that reference google/protobuf well-known types; without them the descriptor
# pool fails with "Depends on file '...timestamp.proto', but it has not been loaded".
from google.protobuf import struct_pb2 as _  # noqa: F401
from google.protobuf import timestamp_pb2 as _t  # noqa: F401

from eidolon_agent.app.transport.grpc.proto import eidolon_pb2 as pb
from eidolon_agent.app.transport.grpc.proto import eidolon_pb2_grpc as pbg

__all__ = ["pb", "pbg"]
