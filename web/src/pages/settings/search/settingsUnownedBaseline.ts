const FORK_CONTROL_REASON = "Fork-added Settings control, not yet in the Settings search catalog.";
const ARCHIVED_TRANSCRIPT_REASON =
  "Static walk reaches live question controls that the read-only archive viewer never renders.";

/**
 * Controls the source guard accepts as unowned, by site: how many there are and why, shared by
 * the guard and the real-page audit. The whole-tree run must find exactly these; a section's run
 * may find no more than the baseline allows.
 */
export const UNOWNED_CONTROLS: Readonly<Record<string, { count: number; reason: string }>> = {
  "src/components/ModelValueCombobox.tsx › ModelValueCombobox › button": {
    count: 1,
    reason: 'Model field\'s combobox; its accessible name includes "*".',
  },
  "src/components/ModelValueCombobox.tsx › ModelValueCombobox › input": {
    count: 1,
    reason: 'Model field\'s combobox; its accessible name includes "*".',
  },
  "src/components/theme/ThemeColorPicker.tsx › ThemeColorPicker › button": {
    count: 1,
    reason: "Theme color picker popover internals, not named by a row.",
  },
  "src/components/theme/ColorSpectrum.tsx › ColorSpectrum › div": {
    count: 1,
    reason: "Theme color picker popover internals, not named by a row.",
  },
  "src/components/theme/ColorSpectrum.tsx › ColorSpectrum › input": {
    count: 1,
    reason: "Theme color picker popover internals, not named by a row.",
  },
  "src/components/theme/ThemeColorPicker.tsx › ThemeColorPicker › input": {
    count: 1,
    reason: "Theme color picker popover internals, not named by a row.",
  },
  "src/pages/settings/HarnessCatalog.tsx › CatalogRow › button": {
    count: 1,
    reason:
      "Skill or plugin row's open button; its region's owner is a prop, which the guard can't read.",
  },
  "src/pages/settings/HarnessCatalog.tsx › PluginPage › SettingsDataRegion": {
    count: 1,
    reason: "Data region's entry is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/HarnessCatalog.tsx › SKILL_MARKDOWN_COMPONENTS › a": {
    count: 1,
    reason: "Link inside a skill's rendered markdown.",
  },
  "src/pages/settings/HarnessCatalog.tsx › SkillPage › SettingsContent": {
    count: 1,
    reason: "Skill content area's entry is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › CredentialCard › button": {
    count: 1,
    reason:
      "Credential card's button; its control is passed in as a prop, which the guard can't read.",
  },
  "src/components/AgentBadgeEditor.tsx › AgentBadgeEditor › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentBadgeEditor.tsx › AgentBadgeEditor › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentBadgeEditor.tsx › AgentBadgeEditor › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentBadgeEditor.tsx › AgentBadgeEditor › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentBadgeEditor.tsx › ColorField › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentBadgeEditor.tsx › ColorField › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentContextTable.tsx › AgentPromptCard › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentContextTable.tsx › AgentPromptCard › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentContextTable.tsx › AgentPromptCard › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentContextTable.tsx › ContextTable › Link": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentContextTable.tsx › PromptTable › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentDefaultsTable › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentDefaultsTable › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentLayerEditor › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentLayerEditor › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentLayerEditor › SelectContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentLayerEditor › SelectItem": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentLayerEditor › SelectTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentDefaultsTable.tsx › AgentLayerEditor › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentEditor.tsx › AgentEditor › Button": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentEditor.tsx › AgentEditor › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentEditor.tsx › AgentEditor › Input": { count: 5, reason: FORK_CONTROL_REASON },
  "src/components/AgentEditor.tsx › AgentEditor › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentEditor.tsx › BuiltinAgentView › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentEditor.tsx › BuiltinAgentView › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentMemberTrigger.tsx › AgentMemberTrigger › DropdownMenuItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentMemberTrigger.tsx › AgentMemberTrigger › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentPicker.tsx › AgentPicker › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentPicker.tsx › AgentPicker › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentPicker.tsx › AgentPicker › entry literal": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentBadgeDisplaySetting › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentSettingsEditor › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentSettingsEditor › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentSettingsEditor › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentSettingsEditor › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentsSettings › Button": {
    count: 10,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentsSettings › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentsSettings › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › AgentsSettings › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/AgentsSettings.tsx › PinToggle › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApiKeysSettings.tsx › ApiKeysPanel › Button": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApiKeysSettings.tsx › ApiKeysPanel › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApiKeysSettings.tsx › ApiKeysPanel › Input": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApiKeysSettings.tsx › ApiKeysPanel › input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApiKeysSettings.tsx › ApiKeysPanel › option": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApiKeysSettings.tsx › ApiKeysPanel › select": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApprovalTimeoutSettings.tsx › ApprovalTimeoutSettings › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ApprovalTimeoutSettings.tsx › ApprovalTimeoutSettings › Switch": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ArchiveWorktreeTierChoices.tsx › ArchiveWorktreeTierChoices › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ArtifactSharingSettings.tsx › ArtifactSharingSettings › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ArtifactSharingSettings.tsx › ArtifactSharingSettings › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ArtifactSharingSettings.tsx › ArtifactSharingSettings › Switch": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/BuiltinPromptEditor.tsx › BuiltinPromptEditor › Button": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/BuiltinPromptEditor.tsx › BuiltinPromptEditor › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CallingDefaultsSection.tsx › CallingDefaultsSection › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CallingDefaultsSection.tsx › CallingDefaultsSection › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CallingDefaultsSection.tsx › CallingDefaultsSection › SelectContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CallingDefaultsSection.tsx › CallingDefaultsSection › SelectItem": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CallingDefaultsSection.tsx › CallingDefaultsSection › SelectTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CallingDefaultsSection.tsx › CallingDefaultsSection › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CallingDefaultsSection.tsx › CallingDefaultsSection › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ChatReserveWidthSetting.tsx › ChatReserveWidthSetting › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ChildIdleSettings.tsx › ChildIdleSettings › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ChildIdleSettings.tsx › ChildIdleSettings › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ChildIdleSettings.tsx › FreezeControls › Switch": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CompactMembership.tsx › CompactMembership › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CompactMembership.tsx › CompactMembership › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/CompactMembership.tsx › MembershipTrigger › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ComposerAttachments.tsx › AttachmentBadge › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ComposerAttachments.tsx › RemoveButton › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ComposerMicButton.tsx › ComposerMicButton › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ConnectedAppsSettings.tsx › ConnectedAppsSettings › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ConnectedAppsSettings.tsx › ConnectedAppsSettings › Input": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ConnectedAppsSettings.tsx › ConnectedAppsSettings › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › ContextUsageSettings › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › ContextUsageSettings › Switch": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › ContextUsageSettings › button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › DraftFields › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › EditorButtons › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › EntryPicker › CommandInput": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › EntryPicker › CommandItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ContextUsageSettings.tsx › EntryPicker › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/DisabledActionTooltip.tsx › DisabledActionTooltip › span": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ExceptionList.tsx › ExceptionList › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ExceptionList.tsx › ExceptionList › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/FileMentionMenu.tsx › FileMentionMenu › button": {
    count: 6,
    reason: ARCHIVED_TRANSCRIPT_REASON,
  },
  "src/components/FileMentionMenu.tsx › FileMentionMenu › div": {
    count: 1,
    reason: ARCHIVED_TRANSCRIPT_REASON,
  },
  "src/components/HelpTip.tsx › HelpTip › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/HelpTip.tsx › HelpTip › button": { count: 1, reason: FORK_CONTROL_REASON },
  "src/components/ImageLightbox.tsx › ImageDownloadButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ImageLightbox.tsx › ZoomableImage › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeepWarmTable.tsx › BulkEditor › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeepWarmTable.tsx › FieldNote › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeepWarmTable.tsx › KeepWarmLevelEditor › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeepWarmTable.tsx › KeepWarmLevelEditor › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeepWarmTable.tsx › ScopedKeepWarmTable › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › DropdownMenuContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › DropdownMenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › Switch": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › option": {
    count: 9,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › select": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › KeyboardShortcutEditor › summary": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/KeyboardShortcutEditor.tsx › ShortcutBindingButton › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/LayeredEditor.tsx › LayeredEditor › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/LayeredEditor.tsx › LayeredEditor › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/LayeredEditor.tsx › LayeredEditorAction › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/LayeredTable.tsx › LayeredTable › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/LayeredTable.tsx › LayeredTable › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/McpRegistry.tsx › McpRegistryConnectionsPage › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/McpRegistry.tsx › ServiceConnection › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ParentSessionsSettings.tsx › ParentSessionsSettings › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ParentSessionsSettings.tsx › ParentSessionsSettings › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ParentSessionsSettings.tsx › ParentSessionsSettings › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/PickerShell.tsx › PickerShell › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/PickerShell.tsx › PickerShell › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/RenderErrorBoundary.tsx › RenderErrorBoundary › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SelectionList.tsx › SelectionList › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SelectionList.tsx › SelectionList › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SelectionList.tsx › SelectionList › button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SelectionList.tsx › SelectionList › div": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SelectionList.tsx › SelectionList › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SelectionList.tsx › SelectionList › section": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionCollabSettings.tsx › HostColorRow › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionCollabSettings.tsx › SessionCollabSettings › Switch": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionCollabSettings.tsx › SessionCollabSettings › option": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionCollabSettings.tsx › SessionCollabSettings › select": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionDefaultModeSelect.tsx › ClaudeFastModeHelp › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionImage.tsx › ImageAttachmentPlaceholder › div": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionNavigationSettings.tsx › GoalMarkersSetting › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionNavigationSettings.tsx › MobileSessionTitleSetting › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionNavigationSettings.tsx › OpenAtLatestSetting › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionNavigationSettings.tsx › SessionNavigationSettings › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SessionNavigationSettings.tsx › SessionNavigationSettings › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SettingsFields.tsx › NumericField › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SettingsPager.tsx › SettingsPager › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SoundAlertSettings.tsx › SoundAlertSettings › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SoundAlertSettings.tsx › SoundAlertSettings › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SoundAlertSettings.tsx › SoundAlertSettings › SelectContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SoundAlertSettings.tsx › SoundAlertSettings › SelectItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SoundAlertSettings.tsx › SoundAlertSettings › SelectTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SoundAlertSettings.tsx › SoundAlertSettings › Switch": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/SoundAlertSettings.tsx › SoundAlertSettings › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/StandingApprovalDefaults.tsx › StandingApprovalDefaults › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/StandingApprovalDefaults.tsx › StandingApprovalDefaults › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VideoPlayer.tsx › VideoPlayerContent › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VideoPlayer.tsx › VideoPlayerContent › DropdownMenuContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VideoPlayer.tsx › VideoPlayerContent › DropdownMenuItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VideoPlayer.tsx › VideoPlayerContent › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VideoPlayer.tsx › VideoPlayerContent › button": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › SettingsSelect › SelectContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › SettingsSelect › SelectItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › SettingsSelect › SelectTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › VoiceInputSettings › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › VoiceInputSettings › SelectContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › VoiceInputSettings › SelectItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › VoiceInputSettings › SelectTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › VoiceInputSettings › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › VoiceInputSettings › Textarea": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/VoiceInputSettings.tsx › VoiceInputSettings › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/MermaidBlock.tsx › MermaidFullscreen › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/MermaidError.tsx › RawDetails › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/message.tsx › ChatCodeBlockCopyButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/message.tsx › ChatCodeBlockPre › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/message.tsx › ChatCodeBlockSendButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/message.tsx › ChatCodeBlockWrapToggle › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/message.tsx › ChatTable › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/message.tsx › MessageAction › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/ai-elements/reasoning.tsx › ReasoningTrigger › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveDateRangePicker.tsx › ArchiveDateRangePicker › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveDateRangePicker.tsx › ArchiveDateRangePicker › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveDateRangePicker.tsx › ArchiveDateRangePicker › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveDateRangePicker.tsx › CalendarGrid › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveDateRangePicker.tsx › CalendarGrid › button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveLibraryToolbar.tsx › ArchiveLibraryToolbar › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveLibraryToolbar.tsx › ArchiveLibraryToolbar › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveLibraryToolbar.tsx › ArchiveLibraryToolbar › PopoverContent": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveLibraryToolbar.tsx › FilterCombobox › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveLibraryToolbar.tsx › FilterCombobox › CommandInput": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveLibraryToolbar.tsx › FilterCombobox › CommandItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveLibraryToolbar.tsx › FilterCombobox › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveTranscriptViewer.tsx › ArchiveTranscriptViewer › Button": {
    count: 14,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveTranscriptViewer.tsx › ArchiveTranscriptViewer › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveTranscriptViewer.tsx › ArchiveTranscriptViewer › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/archive/ArchiveTranscriptViewer.tsx › ArchiveTranscriptViewer › div": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › ApprovalCard › Button": {
    count: 13,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › ApprovalCard › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › ApprovalCard › span": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › ApprovalCard › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › CommandPrefixButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › FileEditApproval › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › SubagentRequesterLine › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › TerminalAccessActions › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ApprovalCard.tsx › TerminalAccessActions › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/AskUserQuestionForm.tsx › AskUserQuestionForm › Button": {
    count: 1,
    reason: ARCHIVED_TRANSCRIPT_REASON,
  },
  "src/components/blocks/AskUserQuestionForm.tsx › QuestionForm › Button": {
    count: 7,
    reason: ARCHIVED_TRANSCRIPT_REASON,
  },
  "src/components/blocks/AskUserQuestionForm.tsx › QuestionForm › input": {
    count: 4,
    reason: ARCHIVED_TRANSCRIPT_REASON,
  },
  "src/components/blocks/AskUserQuestionForm.tsx › QuestionForm › textarea": {
    count: 1,
    reason: ARCHIVED_TRANSCRIPT_REASON,
  },
  "src/components/blocks/BlockRenderer.tsx › TurnWorkedFold › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › FileLinkContextMenu › ContextMenuContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › FileLinkContextMenu › ContextMenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › FileMentionPill › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › PullRequestRefLink › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › RoutedSessionMentionPill › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › ServerSessionLink › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › ServerSessionLink › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › SessionPillLink › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › WorkspaceFileLink › a": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › WorkspaceFileLink › span": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ChatMarkdown.tsx › WorkspacePathInlineCode › code": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ElicitationSchemaForm.tsx › ElicitationSchemaForm › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ElicitationSchemaForm.tsx › ElicitationSchemaForm › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ElicitationSchemaForm.tsx › ElicitationSchemaForm › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ElicitationSchemaForm.tsx › ElicitationSchemaForm › option": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ElicitationSchemaForm.tsx › ElicitationSchemaForm › select": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ExitPlanModeReview.tsx › ExitPlanModeReview › Button": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ExitPlanModeReview.tsx › ExitPlanModeReview › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/PeerMessage.tsx › PeerMessageView › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/SessionMessageCard.tsx › CollapsedSentMessage › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/SessionMessageCard.tsx › ExpandedSentMessage › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/SessionMessageCard.tsx › UnsentMessage › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/SlashCommandCard.tsx › SlashCommandCard › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/SmartRoutingCard.tsx › SmartRoutingCard › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/StatusBlocks.tsx › ErrorBanner › Button": {
    count: 9,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/StatusBlocks.tsx › ErrorBanner › TabsTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/StatusBlocks.tsx › ErrorBanner › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/StatusBlocks.tsx › ErrorForkAction › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/StatusBlocks.tsx › HandoverNote › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/StatusBlocks.tsx › RoutingDecisionCard › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/StatusBlocks.tsx › linkify › a": { count: 1, reason: FORK_CONTROL_REASON },
  "src/components/blocks/SubagentActivityMessage.tsx › SubagentActivityMessage › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/SystemMessage.tsx › SystemMessageView › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/TaskActivityMessage.tsx › TaskActivityMessage › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/TaskActivityMessage.tsx › TaskActivityMessage › span": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/TerminalCommandCard.tsx › TerminalCommandCard › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCard.tsx › GenericToolBody › TabsTrigger": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCard.tsx › RawToggleButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCard.tsx › ToolGroupSummary › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCard.tsx › ToolTitleBody › span": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCard.tsx › ToolTriggerRow › CollapsibleTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCodePanel.tsx › CopyTextButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCodePanel.tsx › OutputSection › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolCodePanel.tsx › WrapTextButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/blocks/ToolValueView.tsx › StringValue › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/chat/AnnotationCards.tsx › AnnotationCards › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/chat/LocalReviewCommentPreview.tsx › LocalReviewCommentPreview › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/chat/UndeliveredMessageNotice.tsx › UndeliveredMessageNotice › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/chat/chatBubbleParts.tsx › CollapsedSessionMessage › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/chat/chatBubbleParts.tsx › PendingUploadRow › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/chat/chatBubbleParts.tsx › UserBubble › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/chat/chatBubbleParts.tsx › useMessageActionVisibility › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/ChatComposer.tsx › ComposerTextarea › textarea": {
    count: 1,
    reason: ARCHIVED_TRANSCRIPT_REASON,
  },
  "src/components/composer/ComposerConfigSections.tsx › ConfigChoices › DropdownMenuCheckboxItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/ComposerConfigSections.tsx › ModelCatalogChoices › DropdownMenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/ComposerConfigSections.tsx › ModelCatalogChoices › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/HarnessPicker.tsx › HarnessPickerConfigPage › DropdownMenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/HarnessPicker.tsx › HarnessPickerConfigRow › DropdownMenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/HarnessPicker.tsx › HarnessPickerConfigRow › DropdownMenuSubTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/HarnessPicker.tsx › HarnessPickerContent › DropdownMenuContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/composer/HarnessPicker.tsx › HarnessPickerSubContent › DropdownMenuSubContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalBarEditor.tsx › SortableKeyTile › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalBarEditor.tsx › SurfaceEditor › Button": {
    count: 7,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalBarEditor.tsx › SurfaceEditor › Switch": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalBarEditor.tsx › SurfaceEditor › button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalBarEditor.tsx › TerminalBarEditor › TabsTrigger": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeyIconPicker.tsx › TerminalKeyIconPicker › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeySheet.tsx › TerminalKeySheet › Button": {
    count: 8,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeySheet.tsx › TerminalKeySheet › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeySheet.tsx › TerminalKeySheet › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeySheet.tsx › TerminalKeySheet › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeySheet.tsx › TerminalKeySheet › option": {
    count: 7,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeySheet.tsx › TerminalKeySheet › select": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/components/settings/TerminalKeySheet.tsx › ToggleRow › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/InstructionsPage.tsx › InstructionsPage › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/InstructionsPage.tsx › InstructionsPage › TabsTrigger": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/InstructionsPage.tsx › InstructionsPage › input": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › AppearanceSection › a": { count: 1, reason: FORK_CONTROL_REASON },
  "src/pages/SettingsPage.tsx › ArchiveTabs › Link": { count: 2, reason: FORK_CONTROL_REASON },
  "src/pages/SettingsPage.tsx › ArchivedBulkActionBar › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ArchivedBulkActionBar › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ArchivedListPane › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ArchivedListPane › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ArchivedListPane › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ArchivedProjectsPane › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ArchivedProjectsPane › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ArchivedRow › Button": { count: 3, reason: FORK_CONTROL_REASON },
  "src/pages/SettingsPage.tsx › ArchivedRow › button": { count: 1, reason: FORK_CONTROL_REASON },
  "src/pages/SettingsPage.tsx › CompactProgressIndicatorControl › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › DeleteWorktreesOnArchiveControl › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › DeleteWorktreesOnArchiveControl › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › ExpandableSettings › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › GeneralSection › Link": { count: 6, reason: FORK_CONTROL_REASON },
  "src/pages/SettingsPage.tsx › InheritLastRightRailTabSetting › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/SettingsPage.tsx › WidenWorkspaceForContentControl › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/TurnRail.tsx › TurnRail › button": { count: 1, reason: FORK_CONTROL_REASON },
  "src/pages/instructions/ChildSessionsPanel.tsx › ChildSessionsPanel › Button": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/ChildSessionsPanel.tsx › ChildSessionsPanel › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/ChildSessionsPanel.tsx › ChildSessionsPanel › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/ChildSessionsPanel.tsx › ChildSessionsPanel › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/ChildSessionsPanel.tsx › ChildSessionsPanel › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/HostPicker.tsx › HostPicker › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/LayerEditor.tsx › LayerEditor › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/LayerEditor.tsx › LayerEditor › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/LayerEditor.tsx › LayerEditor › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/LayerEditor.tsx › LayerEditor › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/LayerGrid.tsx › LayerGrid › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/LayerGrid.tsx › LayerGrid › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RemoveAxisDialog.tsx › RemoveAxisDialog › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RemoveAxisDialog.tsx › RemoveAxisDialog › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RoutePicker.tsx › RoutePicker › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RoutePicker.tsx › RoutePicker › CommandInput": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RoutePicker.tsx › RoutePicker › CommandItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RoutePicker.tsx › RoutePicker › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RouteView.tsx › RouteView › Button": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/instructions/RouteView.tsx › RouteView › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentModelList.tsx › ModelListCard › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentModelList.tsx › ModelListCard › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentModelList.tsx › ModelListCard › p": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentSettingsFolder.tsx › FolderCard › Button": {
    count: 7,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentSettingsFolder.tsx › FolderCard › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentSettingsFolder.tsx › FolderCard › datalist": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentSettingsFolder.tsx › FolderCard › option": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/AgentSettingsFolder.tsx › FolderCard › p": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessCatalog.tsx › BackButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessCatalog.tsx › BackButton › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › ChooseCredential › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › ChooseCredential › div": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › CommandRow › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › CopyButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › CredentialCard › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › DeviceCodeState › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › HarnessFixDialog › Button": {
    count: 15,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › HarnessFixDialog › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/HarnessFixDialog.tsx › SwitchAccountConfirm › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › Choice › SelectContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › Choice › SelectItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › Choice › SelectTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › IconAction › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › ModelProvidersSection › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › ProviderEditor › Button": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › ProviderEditor › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › ProviderEditor › Input": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › ProviderList › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › Usage › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › Usage › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/ModelProvidersSection.tsx › Usage › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/SettingsCustomAgentsSection.tsx › AgentDetail › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › HarnessDetail › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › HarnessDetail › SettingsOwner": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › HarnessWarning › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › SettingsHarnessesSection › SettingsOverlayOwner":
    { count: 3, reason: FORK_CONTROL_REASON },
  "src/pages/settings/SettingsHarnessesSection.tsx › SettingsHarnessesSection › p": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/UsageTelemetrySetting.tsx › UsageTelemetrySetting › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/AccessTab.tsx › AccessTab › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/AccessTab.tsx › AccessTab › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/BookmarksTab.tsx › BookmarksTab › Button": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/BookmarksTab.tsx › BookmarksTab › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/BookmarksTab.tsx › BookmarksTab › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/BrowserSection.tsx › BrowserSection › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/BrowserSection.tsx › BrowserSection › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/ExtensionsTab.tsx › ExtensionsTab › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/ExtensionsTab.tsx › ExtensionsTab › DropdownMenuContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/ExtensionsTab.tsx › ExtensionsTab › DropdownMenuItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/ExtensionsTab.tsx › ExtensionsTab › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/ExtensionsTab.tsx › ExtensionsTab › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/HistoryTab.tsx › HistoryTab › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/HistoryTab.tsx › HistoryTab › SelectContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/HistoryTab.tsx › HistoryTab › SelectItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/HistoryTab.tsx › HistoryTab › SelectTrigger": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/HistoryTab.tsx › HistoryTab › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/SignInTab.tsx › SignInTab › Button": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/SignInTab.tsx › SignInTab › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/SignInTab.tsx › SignInTab › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › Confirm › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › Confirm › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › RowActions › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › RowActions › DropdownMenuContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › RowActions › DropdownMenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › SearchPages › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › SearchPages › InputGroupInput": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/browser/browserSettingsParts.tsx › UpdateReason › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceCandidateCheck.tsx › ChoiceCandidateCheck › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceCandidateCheck.tsx › ChoiceCandidateCheck › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceCandidateCheck.tsx › ChoiceCandidateCheck › option": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceCandidateCheck.tsx › ChoiceCandidateCheck › select": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceCandidateCheck.tsx › ChoiceCandidateCheck › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceConversion.tsx › ChoiceConversion › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceDrag.tsx › ChoiceDraggable › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › ChoiceLimitRules › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › ChoiceLimitRules › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › ChoiceLimitRules › summary": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › NumberPatch › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › NumberPatch › option": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › NumberPatch › select": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › RuleFields › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › RuleFields › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › RuleFields › option": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLimitRules.tsx › RuleFields › select": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLinkEditor.tsx › ChoiceChainEditor › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLinkEditor.tsx › ChoiceLinkEditor › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLinkEditor.tsx › ChoiceLinkEditor › Input": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLinkEditor.tsx › ChoiceLinkEditor › a": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLinkEditor.tsx › ChoiceLinkEditor › datalist": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLinkEditor.tsx › ChoiceLinkEditor › option": {
    count: 10,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoiceLinkEditor.tsx › ChoiceLinkEditor › select": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoicePolicyEditor.tsx › ChoicePolicyEditor › Button": {
    count: 12,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoicePolicyEditor.tsx › ChoicePolicyEditor › Input": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoicePolicyEditor.tsx › ChoicePolicyEditor › Textarea": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoicePolicyEditor.tsx › ChoicePolicyEditor › button": {
    count: 7,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoicePolicyEditor.tsx › ChoicePolicyEditor › option": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoicePolicyEditor.tsx › ChoicePolicyEditor › select": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ChoicePolicyEditor.tsx › ChoicePolicyEditor › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/ProjectSessionChoiceField.tsx › ProjectSessionChoiceField › option":
    { count: 4, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/ProjectSessionChoiceField.tsx › ProjectSessionChoiceField › select":
    { count: 1, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/QuotaFloorControl.tsx › QuotaFloorControl › option": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/QuotaFloorControl.tsx › QuotaFloorControl › select": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/QuotaPreferencesSettings.tsx › QuotaPreferencesSettings › Button":
    { count: 3, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/QuotaPreferencesSettings.tsx › QuotaPreferencesSettings › Input":
    { count: 7, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/QuotaPreferencesSettings.tsx › QuotaPreferencesSettings › button":
    { count: 1, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/QuotaPreferencesSettings.tsx › QuotaPreferencesSettings › datalist":
    { count: 1, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/QuotaPreferencesSettings.tsx › QuotaPreferencesSettings › option":
    { count: 4, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/QuotaPreferencesSettings.tsx › QuotaPreferencesSettings › select":
    { count: 1, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/QuotaPreferencesSettings.tsx › QuotaPreferencesSettings › summary":
    { count: 2, reason: FORK_CONTROL_REASON },
  "src/pages/settings/sessionChoice/SessionChoiceCard.tsx › SessionChoiceCard › PopoverContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceCard.tsx › SessionChoiceCard › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › ReserveExemptWorkTypes › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › ReserveExemptWorkTypes › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › Button": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › Input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › option": {
    count: 14,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › select": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/settings/sessionChoice/SessionChoiceSettings.tsx › SessionChoiceSettings › summary": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/taskBoard/TaskBoardSettings.tsx › TaskBoardSettings › Switch": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/taskBoard/TaskBoardSettings.tsx › TaskBoardSourceSettings › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/taskBoard/TaskBoardSources.tsx › TaskBoardSources › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/taskBoard/TaskBoardSources.tsx › TaskBoardSources › a": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/taskBoard/TaskBoardSources.tsx › TaskBoardSources › input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/taskBoard/TaskBoardSources.tsx › TaskBoardSources › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageBoardSettings.tsx › ColourLine › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageBoardSettings.tsx › ColourLine › input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageBoardSettings.tsx › UsageBoardSettings › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageBoardSettings.tsx › UsageBoardSettings › Link": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageBoardSettings.tsx › UsageBoardSettings › Switch": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageModelPoolsLink.tsx › UsageModelPoolsLink › Link": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageModelPoolsLink.tsx › UsageModelPoolsLink › option": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageModelPoolsLink.tsx › UsageModelPoolsLink › select": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePriceSourceSettings.tsx › UsagePriceSourceSettings › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePriceSourceSettings.tsx › UsagePriceSourceSettings › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePriceSourceSettings.tsx › UsagePriceSourceSettings › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePriceSourceSettings.tsx › UsagePriceSourceSettings › option": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePriceSourceSettings.tsx › UsagePriceSourceSettings › select": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › AccountPriceTable › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › AccountPriceTable › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › AccountPriceTable › option": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › AccountPriceTable › select": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › DecimalField › Input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › PricingEditor › Button": {
    count: 7,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › PricingEditor › Input": {
    count: 5,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › PricingEditor › input": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › PricingEditor › option": {
    count: 10,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › PricingEditor › select": {
    count: 6,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › PricingEditor › summary": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsagePricingPanel.tsx › UsagePricingPanel › summary": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageSourceConnect.tsx › ConnectDialog › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageSourceConnect.tsx › ConnectDialog › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/pages/usageBoard/UsageSourceConnect.tsx › UsageSourceConnect › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/CreateAgentDialog.tsx › CreateAgentDialog › Button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/CreateAgentDialog.tsx › CreateAgentDialog › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/CreateAgentDialog.tsx › CreateAgentDialog › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/HarnessCredentialForm.tsx › AdoptRow › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/HarnessCredentialForm.tsx › HarnessCredentialForm › Input": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/HarnessCredentialForm.tsx › SaveButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/MobileHeaderButton.tsx › MobileHeaderButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorkspacePicker.tsx › PickerIconButton › Button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorkspacePicker.tsx › WorkspacePicker › Button": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorkspacePicker.tsx › WorkspacePicker › button": {
    count: 3,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorkspacePicker.tsx › WorkspacePicker › input": {
    count: 4,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorkspacePickerDialog.tsx › WorkspacePickerDialog › DialogContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorkspacePickerEntry.tsx › WorkspacePickerEntry › MenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorkspacePickerEntry.tsx › WorkspacePickerEntry › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorktreeRadioRow.tsx › WorktreeRadioRow › DropdownMenuContent": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorktreeRadioRow.tsx › WorktreeRadioRow › DropdownMenuItem": {
    count: 2,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorktreeRadioRow.tsx › WorktreeRadioRow › MenuItem": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorktreeRadioRow.tsx › WorktreeRadioRow › button": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
  "src/shell/WorktreeRadioRow.tsx › WorktreeRadioRow › input": {
    count: 1,
    reason: FORK_CONTROL_REASON,
  },
};
