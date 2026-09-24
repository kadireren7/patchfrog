"""Persistence models."""

from dataclasses import dataclass


@dataclass
class Customer:
    id: int
    name: str


@dataclass
class Invoice:
    id: int
    customer_id: int
    gross: int
