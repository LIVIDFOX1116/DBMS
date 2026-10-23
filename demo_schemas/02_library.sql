-- Demo schema 2/3: Library. Same `demo` namespace as 01_student_management.sql
-- -- see that file's header for the isolation rationale, shared by every file
-- in demo_schemas/.

BEGIN;

CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.members (
    member_id       SERIAL PRIMARY KEY,
    full_name       VARCHAR(80)  NOT NULL,
    email           VARCHAR(120) NOT NULL UNIQUE,
    join_date       DATE         NOT NULL,
    membership_type VARCHAR(10)  NOT NULL DEFAULT 'standard'
                      CHECK (membership_type IN ('standard', 'premium'))
);

CREATE TABLE IF NOT EXISTS demo.books (
    book_id           SERIAL PRIMARY KEY,
    isbn              CHAR(13)     NOT NULL UNIQUE,
    title             VARCHAR(150) NOT NULL,
    author            VARCHAR(100) NOT NULL,
    copies_available  SMALLINT     NOT NULL DEFAULT 1 CHECK (copies_available >= 0)
);

CREATE TABLE IF NOT EXISTS demo.loans (
    loan_id     SERIAL PRIMARY KEY,
    member_id   INT  NOT NULL REFERENCES demo.members(member_id),
    book_id     INT  NOT NULL REFERENCES demo.books(book_id),
    loan_date   DATE NOT NULL,
    due_date    DATE NOT NULL,
    return_date DATE,
    CHECK (due_date > loan_date),
    CHECK (return_date IS NULL OR return_date >= loan_date)
);

INSERT INTO demo.members (full_name, email, join_date, membership_type) VALUES
    ('Grace Okafor',   'grace.okafor@example.com',   '2021-03-10', 'premium'),
    ('Hiro Tanaka',    'hiro.tanaka@example.com',    '2022-07-22', 'standard'),
    ('Elena Petrova',  'elena.petrova@example.com',  '2020-11-05', 'premium'),
    ('Samuel Osei',    'samuel.osei@example.com',    '2023-01-18', 'standard'),
    ('Nina Haddad',    'nina.haddad@example.com',    '2023-05-30', 'standard'),
    ('Tom Reilly',     'tom.reilly@example.com',     '2021-09-14', 'premium'),
    ('Amara Diallo',   'amara.diallo@example.com',   '2022-12-02', 'standard')
ON CONFLICT DO NOTHING;

INSERT INTO demo.books (isbn, title, author, copies_available) VALUES
    ('9780132350884', 'Clean Code',                    'Robert C. Martin',  3),
    ('9780201633610', 'Design Patterns',                'Gang of Four',      2),
    ('9781492053013', 'Designing Data-Intensive Apps',  'Martin Kleppmann',  1),
    ('9780596007126', 'Head First Design Patterns',     'Freeman & Robson',  4),
    ('9780262033848', 'Introduction to Algorithms',      'Cormen et al.',     2),
    ('9780135957059', 'The Pragmatic Programmer',        'Hunt & Thomas',     0),
    ('9781491950357', 'Building Microservices',          'Sam Newman',        2),
    ('9780134757599', 'Refactoring',                     'Martin Fowler',     1)
ON CONFLICT DO NOTHING;

INSERT INTO demo.loans (member_id, book_id, loan_date, due_date, return_date) VALUES
    (1, 1, '2024-09-01', '2024-09-22', '2024-09-20'),
    (1, 3, '2024-10-05', '2024-10-26', NULL),
    (2, 2, '2024-09-15', '2024-10-06', '2024-10-01'),
    (3, 5, '2024-08-20', '2024-09-10', '2024-09-10'),
    (4, 6, '2024-10-01', '2024-10-22', NULL),
    (5, 4, '2024-09-28', '2024-10-19', '2024-10-15'),
    (6, 7, '2024-10-10', '2024-10-31', NULL),
    (7, 8, '2024-09-12', '2024-10-03', '2024-09-30'),
    (2, 1, '2024-10-12', '2024-11-02', NULL)
ON CONFLICT DO NOTHING;

COMMIT;
