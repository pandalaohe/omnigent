import { settingsEntry } from "./types";

export const ALWAYS_USE_WORKTREE_ENTRY = settingsEntry({
  id: "always-use-worktree",
  section: "git",
  label: "Always use a random worktree",
  description:
    "Use a fresh, randomly named worktree for new sessions in Git workspaces. Project settings override this default.",
  keywords: ["worktree", "git", "branch", "new session", "isolate"],
});

export const DELETE_WORKTREES_ON_ARCHIVE_ENTRY = settingsEntry({
  id: "delete-worktrees-on-archive",
  available: (availability) => availability.archiveWorktreeCleanup,
  section: "git",
  label: "Worktrees on archive",
  description:
    "Choose when to remove an archived session’s worktree. Source changes and Git branches are kept.",
  keywords: ["worktree", "archive", "delete", "cleanup", "git"],
  parts: {
    ask: "Ask before archiving",
    paths: "Rebuildable ignored directories",
    savePaths: "Save directories",
  },
  presence: {
    paths: { states: ["cleanup enabled"] },
    savePaths: { states: ["cleanup enabled"] },
  },
  focus: "ask",
});

export const DEFAULT_BASE_BRANCH_ENTRY = settingsEntry({
  id: "default-base-branch",
  section: "git",
  label: "Default base branch",
  description: "Prefill the base branch for new worktree branches. Leave blank for no prefill.",
  keywords: ["branch", "main", "base", "worktree", "git"],
});

export const WORKTREE_LOCATION_ENTRY = settingsEntry({
  id: "worktree-location",
  section: "git",
  label: "Worktree location",
  description:
    "Choose where new worktrees are created. Custom templates include the repository and branch. The server validates paths before saving.",
  keywords: ["worktree", "folder", "directory", "template"],
  parts: {
    preset: "Worktree location",
    template: "Worktree location template",
    entry: "Insert project folder",
    repo_parent: "Insert repository parent",
    repo: "Insert repository name",
    branch: "Insert branch name",
    save: "Save location",
    reset: "Use default location",
  },
  partChoices: { preset: ["Next to repository", "Inside project", "Custom"] },
  presence: {
    template: { states: ["custom location"] },
    entry: { states: ["custom location"] },
    repo_parent: { states: ["custom location"] },
    repo: { states: ["custom location"] },
    branch: { states: ["custom location"] },
  },
  reachedBy: {
    template: "preset",
    entry: "preset",
    repo_parent: "preset",
    repo: "preset",
    branch: "preset",
  },
  focus: "preset",
});

export const GIT_SETTINGS = [
  ALWAYS_USE_WORKTREE_ENTRY,
  DEFAULT_BASE_BRANCH_ENTRY,
  DELETE_WORKTREES_ON_ARCHIVE_ENTRY,
  WORKTREE_LOCATION_ENTRY,
] as const;
