"""Personal-key schema uses the same migration chain on every supported backend."""

import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine


def test_user_api_keys_schema(db_uri):
    engine = get_or_create_engine(db_uri)
    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("user_api_keys")}
    assert columns == {
        "workspace_id",
        "id",
        "user_id",
        "name",
        "prefix",
        "secret_sha256",
        "scopes",
        "created_at",
        "expires_at",
        "last_used_at",
        "regenerated_at",
    }
    indexes = {index["name"]: index for index in inspector.get_indexes("user_api_keys")}
    assert indexes["ix_user_api_keys_digest"]["unique"]
    assert indexes["ix_user_api_keys_name"]["column_names"] == ["workspace_id", "user_id", "name"]
