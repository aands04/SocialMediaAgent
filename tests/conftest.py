import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base


@pytest.fixture
def db(tmp_path):
 e=create_engine(f"sqlite:///{tmp_path/'test.db'}")
 Base.metadata.create_all(e)
 with sessionmaker(e,expire_on_commit=False)() as s: yield s
@pytest.fixture
def media_root(tmp_path):
 p=tmp_path/"media"; p.mkdir(); return p
