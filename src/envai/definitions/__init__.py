"""Portable desired-state definitions and conservative deployment reconciliation."""
from .compiler import DefinitionError, Manifest, canonical_hash, compile_manifest
from .service import (
    AmbiguousMutation,
    ClientRemote,
    Definitions,
    DriftError,
    Remote,
    ResourceIdentity,
    SyncOptions,
)
from .stores import FileStore, MemoryStore, MySQLStore, PostgresStore, SQLiteStore
from .types import AgentDefinition, ManifestDocument, TeamDefinition, TeamMember, ToolDefinition

__all__ = [
    "AgentDefinition", "ManifestDocument", "TeamDefinition", "TeamMember", "ToolDefinition",
    "AmbiguousMutation", "ClientRemote", "DefinitionError", "Definitions", "DriftError",
    "FileStore", "Manifest", "MemoryStore", "MySQLStore", "PostgresStore", "SQLiteStore", "Remote", "SyncOptions",
    "canonical_hash", "compile_manifest", "ResourceIdentity",
]
