-- Proof-of-life queries for the 3 base demo schemas: run these after
-- `python demo_schemas/seed_demo.py` to show real, joined, cross-table
-- results (not just empty tables). Run one query at a time -- see
-- REVIEW_GUIDE.md's "Live Demo: Schema Management" section for the order.

-- 1) Student management: each student's enrolled courses and grades.
SELECT s.full_name, c.course_code, c.title, e.semester, e.grade
FROM demo.enrollments e
JOIN demo.students s ON s.student_id = e.student_id
JOIN demo.courses  c ON c.course_id = e.course_id
ORDER BY s.full_name, e.semester;

-- 2) Library: books currently on loan (not yet returned), with who has them.
SELECT b.title, b.author, m.full_name AS borrower, l.loan_date, l.due_date
FROM demo.loans l
JOIN demo.books   b ON b.book_id = l.book_id
JOIN demo.members m ON m.member_id = l.member_id
WHERE l.return_date IS NULL
ORDER BY l.due_date;

-- 3) E-commerce: order totals per customer (join across 4 tables).
SELECT c.full_name, o.order_id, o.status,
       SUM(oi.quantity * oi.unit_price) AS order_total
FROM demo.orders o
JOIN demo.customers  c  ON c.customer_id = o.customer_id
JOIN demo.order_items oi ON oi.order_id = o.order_id
GROUP BY c.full_name, o.order_id, o.status
ORDER BY o.order_id;
