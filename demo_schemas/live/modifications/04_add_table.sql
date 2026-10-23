-- LIVE DEMO modification 4/5: add a new table.
--   python demo_schemas/demo_db.py load demo_schemas/live/modifications/04_add_table.sql
CREATE TABLE demo.product_reviews (
    review_id   SERIAL PRIMARY KEY,
    product_id  INT NOT NULL REFERENCES demo.products(product_id),
    customer_id INT NOT NULL REFERENCES demo.customers(customer_id),
    rating      SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     VARCHAR(500),
    review_date DATE NOT NULL DEFAULT CURRENT_DATE
);
