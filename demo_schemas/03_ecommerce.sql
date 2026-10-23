-- Demo schema 3/3: E-commerce. Same `demo` namespace -- see
-- 01_student_management.sql's header for the isolation rationale.
--
-- This is the schema the live modification demo (demo_schemas/live/modifications/)
-- alters: add a column, rename it, add a constraint, add a table, drop it.

BEGIN;

CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.customers (
    customer_id SERIAL PRIMARY KEY,
    full_name   VARCHAR(80)  NOT NULL,
    email       VARCHAR(120) NOT NULL UNIQUE,
    signup_date DATE         NOT NULL
);

CREATE TABLE IF NOT EXISTS demo.products (
    product_id SERIAL PRIMARY KEY,
    name       VARCHAR(120) NOT NULL,
    category   VARCHAR(60)  NOT NULL,
    price      DECIMAL(10,2) NOT NULL CHECK (price > 0),
    stock      INT NOT NULL DEFAULT 0 CHECK (stock >= 0)
);

CREATE TABLE IF NOT EXISTS demo.orders (
    order_id     SERIAL PRIMARY KEY,
    customer_id  INT NOT NULL REFERENCES demo.customers(customer_id),
    order_date   DATE NOT NULL,
    status       VARCHAR(10) NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS demo.order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id      INT NOT NULL REFERENCES demo.orders(order_id),
    product_id    INT NOT NULL REFERENCES demo.products(product_id),
    quantity      INT NOT NULL CHECK (quantity > 0),
    unit_price    DECIMAL(10,2) NOT NULL CHECK (unit_price > 0),
    UNIQUE (order_id, product_id)
);

INSERT INTO demo.customers (full_name, email, signup_date) VALUES
    ('Olivia Brandt',  'olivia.brandt@example.com',  '2022-02-11'),
    ('Kenji Watanabe', 'kenji.watanabe@example.com', '2022-06-19'),
    ('Isabella Russo', 'isabella.russo@example.com', '2023-03-04'),
    ('Mohammed Farouk','mohammed.farouk@example.com','2021-11-27'),
    ('Clara Nilsson',  'clara.nilsson@example.com',  '2023-09-15'),
    ('Victor Alves',   'victor.alves@example.com',   '2022-12-01'),
    ('Yuki Sato',      'yuki.sato@example.com',      '2024-01-22'),
    ('Diego Morales',  'diego.morales@example.com',  '2023-07-08')
ON CONFLICT DO NOTHING;

INSERT INTO demo.products (name, category, price, stock) VALUES
    ('Wireless Mouse',        'Electronics', 24.99, 150),
    ('Mechanical Keyboard',   'Electronics', 79.99, 60),
    ('USB-C Hub',             'Electronics', 34.50, 90),
    ('Standing Desk',         'Furniture',   349.00, 15),
    ('Office Chair',          'Furniture',   219.99, 22),
    ('Notebook Set (3-pack)', 'Stationery',  12.75, 300),
    ('Fountain Pen',          'Stationery',  18.00, 120),
    ('Desk Lamp',             'Furniture',   45.25, 40)
ON CONFLICT DO NOTHING;

INSERT INTO demo.orders (customer_id, order_date, status) VALUES
    (1, '2024-09-02', 'delivered'),
    (2, '2024-09-10', 'delivered'),
    (3, '2024-09-18', 'shipped'),
    (4, '2024-09-20', 'cancelled'),
    (1, '2024-10-01', 'shipped'),
    (5, '2024-10-03', 'pending'),
    (6, '2024-10-05', 'delivered'),
    (7, '2024-10-08', 'pending'),
    (8, '2024-10-10', 'shipped'),
    (2, '2024-10-12', 'pending')
ON CONFLICT DO NOTHING;

INSERT INTO demo.order_items (order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 2, 24.99),
    (1, 6, 1, 12.75),
    (2, 2, 1, 79.99),
    (3, 4, 1, 349.00),
    (3, 5, 1, 219.99),
    (4, 7, 3, 18.00),
    (5, 3, 2, 34.50),
    (6, 8, 1, 45.25),
    (7, 1, 1, 24.99),
    (7, 2, 1, 79.99),
    (8, 6, 4, 12.75),
    (9, 4, 1, 349.00),
    (10, 7, 2, 18.00)
ON CONFLICT DO NOTHING;

COMMIT;
