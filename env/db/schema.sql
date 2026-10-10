PRAGMA foreign_keys = ON;

CREATE TABLE inventory_items (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    on_hand INTEGER NOT NULL CHECK (on_hand >= 0),
    threshold INTEGER NOT NULL CHECK (threshold >= 0),
    target INTEGER NOT NULL CHECK (target >= 0),
    max_stock INTEGER NOT NULL CHECK (max_stock >= 0),
    expiry_date TEXT CHECK (expiry_date IS NULL OR date(expiry_date) IS NOT NULL)
);

CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended')),
    lead_days INTEGER NOT NULL CHECK (lead_days >= 0),
    minimum_order_value_inr INTEGER NOT NULL CHECK (minimum_order_value_inr >= 0)
);

CREATE TABLE supplier_delivery_days (
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 5),
    PRIMARY KEY (supplier_id, weekday)
);

CREATE TABLE supplier_catalog (
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    sku TEXT NOT NULL REFERENCES inventory_items(sku),
    unit_price_inr INTEGER NOT NULL CHECK (unit_price_inr > 0),
    pack_size INTEGER NOT NULL CHECK (pack_size > 0),
    PRIMARY KEY (supplier_id, sku)
);

CREATE TABLE purchase_orders (
    po_number TEXT PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES inventory_items(sku),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_inr INTEGER NOT NULL CHECK (unit_price_inr > 0),
    status TEXT NOT NULL CHECK (
        status IN ('Placed', 'Confirmed', 'In Transit', 'Cancelled', 'Delivered')
    ),
    created_date TEXT NOT NULL CHECK (date(created_date) IS NOT NULL),
    expected_delivery TEXT NOT NULL CHECK (date(expected_delivery) IS NOT NULL),
    created_by TEXT,
    notes TEXT
);
