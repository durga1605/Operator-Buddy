"""
Database connection utilities for the Core application of the Traceability project.
"""

from django.conf import settings
from pymongo import MongoClient


def get_db_connection(plant_code):
    """
    Establish and return a secure connection to the MongoDB 'trace' database based on plant code.
    """
    if plant_code == "002":
        print("Connecting to Testdb for plant code 002")
        uri = settings.COE_DB
        db_name = "Testdb"
    elif plant_code == "034":
        uri = settings.PGR_DB
        db_name = "pgr_trace"

    elif plant_code == "143":
        uri = settings.VVP_DB
        db_name = "LGB_DB"

    elif plant_code == "123":
        uri = settings.PPM_DB
        db_name = "trace"

    elif plant_code == "041":
        uri = settings.KPM_DB
        db_name = "trace"
        # db_name = "kpmqa_trace"

    else:
        raise ValueError(f"Unsupported plant code: {plant_code}")

    client = MongoClient(uri, serverSelectionTimeoutMS=2000)
    db = client[db_name]
    return db
