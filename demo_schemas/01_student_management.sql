-- Demo schema 1/3: Student management.
-- Lives in the isolated `demo` Postgres schema namespace -- never touches
-- FrameCheck's own tables (those are all in `public`). Safe to drop and
-- recreate at any time: see demo_schemas/demo_db.py reset.

BEGIN;

CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.students (
    student_id      SERIAL PRIMARY KEY,
    full_name       VARCHAR(80)  NOT NULL,
    email           VARCHAR(120) NOT NULL UNIQUE,
    enrollment_date DATE         NOT NULL,
    gpa             DECIMAL(3,2) CHECK (gpa >= 0.0 AND gpa <= 4.0)
);

CREATE TABLE IF NOT EXISTS demo.courses (
    course_id   SERIAL PRIMARY KEY,
    course_code VARCHAR(10)  NOT NULL UNIQUE,
    title       VARCHAR(100) NOT NULL,
    department  VARCHAR(60)  NOT NULL,
    credits     SMALLINT     NOT NULL CHECK (credits > 0 AND credits <= 6)
);

CREATE TABLE IF NOT EXISTS demo.enrollments (
    enrollment_id SERIAL PRIMARY KEY,
    student_id    INT NOT NULL REFERENCES demo.students(student_id),
    course_id     INT NOT NULL REFERENCES demo.courses(course_id),
    semester      VARCHAR(20) NOT NULL,
    grade         CHAR(2) CHECK (grade IN ('A','A-','B+','B','B-','C+','C','C-','D','F') OR grade IS NULL),
    UNIQUE (student_id, course_id, semester)
);

INSERT INTO demo.students (full_name, email, enrollment_date, gpa) VALUES
    ('Asha Rao',        'asha.rao@example.edu',        '2023-08-15', 3.80),
    ('Liam Chen',       'liam.chen@example.edu',        '2023-08-15', 3.20),
    ('Priya Nair',      'priya.nair@example.edu',       '2022-08-20', 3.95),
    ('Marcus Webb',     'marcus.webb@example.edu',      '2024-01-10', 2.75),
    ('Sofia Torres',    'sofia.torres@example.edu',     '2023-08-15', 3.40),
    ('David Kim',       'david.kim@example.edu',        '2022-08-20', 3.60),
    ('Fatima Siddiqui', 'fatima.siddiqui@example.edu',  '2024-01-10', 3.10),
    ('Noah Becker',     'noah.becker@example.edu',      '2023-08-15', 2.90)
ON CONFLICT DO NOTHING;

INSERT INTO demo.courses (course_code, title, department, credits) VALUES
    ('CS101', 'Introduction to Programming',    'Computer Science', 4),
    ('CS301', 'Database Management Systems',    'Computer Science', 4),
    ('CS450', 'Distributed Systems',            'Computer Science', 3),
    ('MA201', 'Discrete Mathematics',           'Mathematics',      3),
    ('EC110', 'Principles of Microeconomics',   'Economics',        3),
    ('ST220', 'Statistics for Data Science',    'Statistics',       4)
ON CONFLICT DO NOTHING;

INSERT INTO demo.enrollments (student_id, course_id, semester, grade) VALUES
    (1, 1, 'Fall 2023', 'A'),
    (1, 2, 'Spring 2024', 'A-'),
    (2, 1, 'Fall 2023', 'B+'),
    (2, 4, 'Fall 2023', 'B'),
    (3, 2, 'Spring 2024', 'A'),
    (3, 3, 'Fall 2024', NULL),
    (4, 1, 'Spring 2024', 'C+'),
    (5, 4, 'Fall 2023', 'B-'),
    (6, 2, 'Spring 2024', 'A-'),
    (7, 1, 'Spring 2024', 'B'),
    (8, 5, 'Fall 2023', 'B+')
ON CONFLICT DO NOTHING;

COMMIT;
