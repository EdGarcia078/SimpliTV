import sqlite3
import os
from pathlib import Path

DB_PATH = Path("simplitv.db")

def migrate():
    if not DB_PATH.exists():
        print("No DB found, nothing to migrate.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create channels table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR NOT NULL UNIQUE,
            batch_size INTEGER NOT NULL,
            start_from_even BOOLEAN NOT NULL,
            loop BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL
        )
    """)
    
    # Check if episodes has channel_id
    cursor.execute("PRAGMA table_info(episodes)")
    columns = [col[1] for col in cursor.fetchall()]
    if "channel_id" not in columns:
        print("Migrating episodes table...")
        # Add column
        cursor.execute("ALTER TABLE episodes ADD COLUMN channel_id INTEGER REFERENCES channels(id)")
    
    # Check if channel_state has channel_id
    cursor.execute("PRAGMA table_info(channel_state)")
    columns = [col[1] for col in cursor.fetchall()]
    if "channel_id" not in columns:
        print("Migrating channel_state table...")
        # Add column
        cursor.execute("ALTER TABLE channel_state ADD COLUMN channel_id INTEGER REFERENCES channels(id)")
        
        # Copy id to channel_id if it exists
        cursor.execute("UPDATE channel_state SET channel_id = id")
        
        # Rename channel_state to channel_state_old
        cursor.execute("ALTER TABLE channel_state RENAME TO channel_state_old")
        
        # We will let SQLModel create the new one, but let's just recreate it here with the right schema
        cursor.execute("""
            CREATE TABLE channel_state (
                channel_id INTEGER NOT NULL PRIMARY KEY,
                current_episode_id INTEGER NOT NULL,
                consecutive_plays INTEGER NOT NULL DEFAULT 1,
                next_episode_id INTEGER,
                started_at DATETIME NOT NULL,
                duration FLOAT NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels (id),
                FOREIGN KEY(current_episode_id) REFERENCES episodes (id),
                FOREIGN KEY(next_episode_id) REFERENCES episodes (id)
            )
        """)
        cursor.execute("""
            INSERT INTO channel_state (channel_id, current_episode_id, consecutive_plays, next_episode_id, started_at, duration, updated_at)
            SELECT channel_id, current_episode_id, 1, next_episode_id, started_at, duration, updated_at
            FROM channel_state_old
            WHERE channel_id IS NOT NULL
        """)
        cursor.execute("DROP TABLE channel_state_old")

    # Insert a Default channel if none exists
    cursor.execute("SELECT COUNT(*) FROM channels")
    if cursor.fetchone()[0] == 0:
        print("Inserting Default channel...")
        cursor.execute("""
            INSERT INTO channels (name, batch_size, start_from_even, loop, created_at)
            VALUES ('Default', 1, 0, 1, CURRENT_TIMESTAMP)
        """)
        cursor.execute("UPDATE episodes SET channel_id = 1 WHERE channel_id IS NULL")

    conn.commit()
    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
