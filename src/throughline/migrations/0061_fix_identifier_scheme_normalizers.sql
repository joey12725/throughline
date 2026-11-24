-- Fix identifier_schemes normalizers for aliased phone and email schemes.
-- Previously sms/imessage/signal/whatsapp phone schemes mapped to
-- (phone, number) with normalizer='trim', keeping '+' in value_norm while
-- direct (phone, number) imports used normalizer='phone' (digits only),
-- splitting confirmed contacts from their ingested SMS messages.

UPDATE tl.identifier_schemes
SET normalizer = 'phone'
WHERE same_system = 'phone' AND same_kind = 'number' AND normalizer = 'trim';

UPDATE tl.identifier_schemes
SET normalizer = 'email'
WHERE same_system = 'email' AND same_kind = 'address' AND normalizer = 'trim';
