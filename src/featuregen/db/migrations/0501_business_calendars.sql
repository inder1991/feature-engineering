-- business_calendars — phase-05-owned calendar store for §5.5 timer resolution.
-- Not part of the shared DDL; redefines nothing.
CREATE TABLE IF NOT EXISTS business_calendars (
    calendar_name text        PRIMARY KEY,
    timezone      text        NOT NULL DEFAULT 'UTC',
    workdays      integer[]   NOT NULL DEFAULT '{1,2,3,4,5}',   -- ISO weekday 1=Mon..7=Sun
    holidays      date[]      NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now()
);
