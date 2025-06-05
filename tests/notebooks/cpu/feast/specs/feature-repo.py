# Copyright 2025 Canonical Ltd.
# Licensed under the Apache License, Version 2.0

"""Example feature repo for Feast."""

from datetime import timedelta

from feast import Entity, FeatureService, FeatureView, Field
from feast.infra.offline_stores.contrib.postgres_offline_store.postgres_source import (
    PostgreSQLSource,
)
from feast.types import Float32, Int64

# Define an entity for the driver
driver = Entity(name="driver", join_keys=["driver_id"])

# Define the source from PostgreSQL
driver_stats_source = PostgreSQLSource(
    name="driver_hourly_stats_source",
    query="SELECT * FROM driver_stats",
    timestamp_field="event_timestamp",
    created_timestamp_column="created",
)

# Define the feature view
driver_stats_fv = FeatureView(
    name="driver_hourly_stats2",
    entities=[driver],
    ttl=timedelta(days=1),
    schema=[
        Field(name="conv_rate", dtype=Float32),
        Field(name="acc_rate", dtype=Float32),
        Field(name="avg_daily_trips", dtype=Int64, description="Average daily trips"),
    ],
    online=True,
    source=driver_stats_source,
    tags={"team": "driver_performance"},
)

# Group features into a model version
driver_activity_v1 = FeatureService(
    name="driver_activity_v1",
    features=[driver_stats_fv],
)
