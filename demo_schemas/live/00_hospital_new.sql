-- LIVE DEMO: a schema not yet loaded. Run this one live with:
--   python demo_schemas/demo_db.py load demo_schemas/live/00_hospital_new.sql
-- Same `demo` namespace as the 3 base schemas -- see
-- demo_schemas/01_student_management.sql's header for the isolation
-- rationale. Independent domain on purpose: proves the tool accepts an
-- arbitrary new schema, not just the 3 pre-staged ones.

BEGIN;

CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.doctors (
    doctor_id SERIAL PRIMARY KEY,
    full_name VARCHAR(80) NOT NULL,
    specialty VARCHAR(60) NOT NULL
);

CREATE TABLE IF NOT EXISTS demo.patients (
    patient_id SERIAL PRIMARY KEY,
    full_name  VARCHAR(80) NOT NULL,
    date_of_birth DATE NOT NULL,
    blood_type VARCHAR(3) CHECK (blood_type IN ('A+','A-','B+','B-','AB+','AB-','O+','O-'))
);

CREATE TABLE IF NOT EXISTS demo.appointments (
    appointment_id   SERIAL PRIMARY KEY,
    patient_id       INT NOT NULL REFERENCES demo.patients(patient_id),
    doctor_id        INT NOT NULL REFERENCES demo.doctors(doctor_id),
    appointment_date TIMESTAMP NOT NULL,
    status           VARCHAR(10) NOT NULL DEFAULT 'scheduled'
                       CHECK (status IN ('scheduled', 'completed', 'cancelled'))
);

INSERT INTO demo.doctors (full_name, specialty) VALUES
    ('Dr. Amina Yusuf',    'Cardiology'),
    ('Dr. Ben Fischer',    'Pediatrics'),
    ('Dr. Laura Mendes',   'Orthopedics'),
    ('Dr. Raj Kapoor',     'General Practice');

INSERT INTO demo.patients (full_name, date_of_birth, blood_type) VALUES
    ('Ethan Walsh',    '1988-04-12', 'O+'),
    ('Mei Lin',         '1995-11-03', 'A-'),
    ('Carlos Ibanez',   '1972-06-25', 'B+'),
    ('Ingrid Larsen',   '2001-01-19', 'AB+'),
    ('Tunde Balogun',   '1960-09-08', 'O-'),
    ('Ava Johansson',   '1990-03-30', 'A+');

INSERT INTO demo.appointments (patient_id, doctor_id, appointment_date, status) VALUES
    (1, 1, '2024-10-15 09:00', 'completed'),
    (2, 2, '2024-10-16 10:30', 'completed'),
    (3, 3, '2024-10-18 14:00', 'scheduled'),
    (4, 4, '2024-10-18 15:00', 'scheduled'),
    (5, 1, '2024-10-20 11:00', 'scheduled'),
    (6, 2, '2024-09-29 13:15', 'cancelled'),
    (1, 4, '2024-11-02 09:30', 'scheduled'),
    (3, 1, '2024-11-05 10:00', 'scheduled');

COMMIT;
