"""
Neon Schema Migration — Add product_tier and bundle_grouping columns to products table.
Run once. Safe to re-run (uses IF NOT EXISTS logic).
"""
import sys
sys.path.insert(0, '/home/ubuntu/ffl-check')
from credentials import get_sqlalchemy_engine
from sqlalchemy import text

engine = get_sqlalchemy_engine()

migration_sql = """
-- Add product_tier column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='products' AND column_name='product_tier'
    ) THEN
        ALTER TABLE products ADD COLUMN product_tier VARCHAR(50);
        RAISE NOTICE 'Added product_tier column';
    ELSE
        RAISE NOTICE 'product_tier column already exists';
    END IF;
END $$;

-- Add bundle_grouping column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='products' AND column_name='bundle_grouping'
    ) THEN
        ALTER TABLE products ADD COLUMN bundle_grouping VARCHAR(50);
        RAISE NOTICE 'Added bundle_grouping column';
    ELSE
        RAISE NOTICE 'bundle_grouping column already exists';
    END IF;
END $$;
"""

try:
    with engine.connect() as conn:
        # Check if products table exists first
        result = conn.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'products'
            )
        """))
        table_exists = result.scalar()
        
        if not table_exists:
            print("products table does not exist yet — creating it with all columns including new ones")
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    sku VARCHAR(100) UNIQUE,
                    collection_id VARCHAR(50),
                    palette_name VARCHAR(100),
                    fractal_type VARCHAR(50),
                    d_value FLOAT,
                    drive_file_id VARCHAR(200),
                    etsy_listing_id VARCHAR(100),
                    shopify_product_id VARCHAR(100),
                    printful_sync_product_id VARCHAR(100),
                    status VARCHAR(50),
                    product_tier VARCHAR(50),
                    bundle_grouping VARCHAR(50),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """))
            conn.commit()
            print("products table created successfully with product_tier and bundle_grouping columns")
        else:
            print("products table exists — running column additions")
            # Run migrations individually to avoid DO $$ block issues with pg8000
            # Check and add product_tier
            result = conn.execute(text("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='products' AND column_name='product_tier'
            """))
            if result.scalar() == 0:
                conn.execute(text("ALTER TABLE products ADD COLUMN product_tier VARCHAR(50)"))
                conn.commit()
                print("Added product_tier column")
            else:
                print("product_tier column already exists")
            
            # Check and add bundle_grouping
            result = conn.execute(text("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='products' AND column_name='bundle_grouping'
            """))
            if result.scalar() == 0:
                conn.execute(text("ALTER TABLE products ADD COLUMN bundle_grouping VARCHAR(50)"))
                conn.commit()
                print("Added bundle_grouping column")
            else:
                print("bundle_grouping column already exists")
        
        # Verify final schema
        result = conn.execute(text("""
            SELECT column_name, data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'products'
            ORDER BY ordinal_position
        """))
        print("\nFinal products table schema:")
        for row in result:
            print(f"  {row[0]}: {row[1]}({row[2] or ''})")

except Exception as e:
    print(f"Migration error: {e}")
    import traceback
    traceback.print_exc()
