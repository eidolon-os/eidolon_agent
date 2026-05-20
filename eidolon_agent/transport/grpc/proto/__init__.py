"""Generated gRPC stubs for eidolon.proto.

Generated files are git-ignored; rebuild with::

    python -m grpc_tools.protoc -I eidolon_agent/transport/grpc/proto \
        --python_out=eidolon_agent/transport/grpc/proto \
        --grpc_python_out=eidolon_agent/transport/grpc/proto \
        --pyi_out=eidolon_agent/transport/grpc/proto \
        eidolon_agent/transport/grpc/proto/eidolon.proto

The bootstrap script (``deploy/dev/init.sh``) runs the build automatically.
"""

from eidolon_agent.transport.grpc.proto import eidolon_pb2 as pb
from eidolon_agent.transport.grpc.proto import eidolon_pb2_grpc as pbg

__all__ = ["pb", "pbg"]
