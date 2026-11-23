from typing import ClassVar, Optional

from sqlmodel import Field, Relationship, SQLModel


class TeamBase(SQLModel):
    name: str = Field(index=True)


class Team(TeamBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    heroes: list["Hero"] = Relationship(back_populates="team")


class Hero(SQLModel, table=True):
    __tablename__ = "heroes"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    age: Optional[int] = None
    registry_note: ClassVar[str] = "not a column"
    team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    team: Optional[Team] = Relationship(back_populates="heroes", sa_relationship_kwargs={"lazy": "selectin"})


class HeroCreate(TeamBase):
    secret: str
