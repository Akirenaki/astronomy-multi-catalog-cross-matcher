#!/usr/bin/env python3
"""
Utility script to reset the database schema for development.
Run this when the SQLAlchemy models have changed and the database schema needs to be updated.
WARNING: This will drop all tables and recreate them, deleting any cached data.
"""

import asyncio
import sys

from app.database import reset_db


async def main():
    print("⚠️  This will delete all data in the database and recreate the schema.")
    response = input("Are you sure? Type 'yes' to confirm: ")
    if response.lower() != "yes":
        print("Cancelled.")
        sys.exit(0)
    
    print("Resetting database schema...")
    await reset_db()
    print("✅ Database schema reset complete!")


if __name__ == "__main__":
    asyncio.run(main())
