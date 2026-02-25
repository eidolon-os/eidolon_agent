"""SQLAlchemy 声明式基类与表名约定."""

from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass


class Base(DeclarativeBase):
    """所有 ORM 模型的基类."""

    pass
