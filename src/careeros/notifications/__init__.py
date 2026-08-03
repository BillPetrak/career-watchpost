"""Local desktop notification delivery for due reminders.

CareerOS notifies you locally when a stored reminder comes due. It never
contacts employers, never submits applications, never sends email, and never
uses any external service; notification delivery is a local desktop pop-up (or,
in headless/test environments, a deterministic stdout line). Delivery is a
separate fact from reminder completion and never completes a reminder or
touches application status.
"""
