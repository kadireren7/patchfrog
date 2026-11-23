from sqlalchemy import Column, ForeignKey, Integer, String, Table
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

post_tags = Table(
    "post_tags",
    Base.metadata,
    Column("post_id", ForeignKey("posts.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)


class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    title = Column("post_title", String(200))
    tags = relationship("Tag", secondary=post_tags, lazy="joined")
    meta = relationship("PostMeta", uselist=False, backref="post")


class Tag(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True)


class PostMeta(Base):
    __tablename__ = "post_meta"
    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id"))
