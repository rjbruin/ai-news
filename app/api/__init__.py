"""JSON API blueprint.

Exposes summary scope items, news items, editions, agent memory, and the
feedback endpoint. Backs the UI and gives the agent's tools a documented HTTP
surface for the same data (the in-process tools call the service layer directly).
"""
