/** Gateway DTOs retained from the retired React prototype.
 * These types do not authenticate callers or grant access to a project/plugin.
 * Keep them aligned with services/gateway/app/main.py and history_store.py.
 */
export type RouteMode = 'auto' | 'dsh' | 'enterprise'
export type ChatRoute = 'dsh_chat' | 'enterprise_capability'

export type ChatRouteRequest = {
  input: string
  conversation_id: string
  route_mode?: RouteMode
  project_key?: string | null
  plugin_key?: string | null
  file_ids?: string[]
  provider_key?: string
  model?: string
}

/** Untrusted client selection; the Gateway must resolve authorization again. */
export type ProjectContext = {
  routeMode: RouteMode
  projectKey?: string
  pluginKey?: string
  fileIds: string[]
}

export type ConversationSummary = {
  conversation_id: string
  title: string
  route: ChatRoute
  project_key?: string | null
  project_name?: string | null
  last_activity_at: string
  expires_at?: string | null
}
