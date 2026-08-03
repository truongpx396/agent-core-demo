-- Create the litellm database alongside the langfuse one.
-- This runs once on first postgres container startup.
CREATE DATABASE litellm OWNER langfuse;
