'use strict';

const byId = (id) => document.getElementById(id);
const credentialsDialog = byId('credentials-dialog');
const connectDialog = byId('connect-dialog');
let activeCredentials = null;
let editingApp = null;
let coolifyApps = [];
let coolifyAvailable = false;
let toastHandle;

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
  });
  let result = null;
  try { result = await response.json(); } catch (_) { /* generic HTTP error below */ }
  if (!response.ok) {
    const detail = result?.detail;
    let message = typeof detail === 'string' ? detail : `Request failed (HTTP ${response.status})`;
    if (Array.isArray(detail)) message = detail.map((e) => e.msg).join('; ');
    throw new Error(message);
  }
  return result;
}

function notify(message, error = false) {
  const el = byId('toast');
  el.textContent = message;
  el.classList.toggle('error', error);
  el.classList.add('visible');
  clearTimeout(toastHandle);
  toastHandle = setTimeout(() => el.classList.remove('visible'), error ? 9000 : 5500);
}

function elt(tag, cls, content) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (content !== undefined) node.textContent = content;
  return node;
}

function button(text, className, callback) {
  const node = elt('button', className, text);
  node.type = 'button';
  node.addEventListener('click', async () => {
    node.disabled = true;
    try { await callback(); } catch (error) { notify(error.message, true); }
    finally { node.disabled = false; }
  });
  return node;
}

function coolifyName(uuid) {
  return coolifyApps.find((app) => app.uuid === uuid)?.path ?? uuid;
}

function fillChoices(select, includeStandalone = false) {
  const current = select.value;
  select.replaceChildren();
  if (includeStandalone) select.append(new Option('Standalone database (no Coolify connection)', ''));
  else select.append(new Option('Choose a Coolify application…', ''));
  for (const app of coolifyApps) {
    // The complete path stays visible even after selection. UUID is the actual target.
    const label = app.selectable
      ? `${app.path} · ${app.fqdn || app.uuid.slice(0, 8)}`
      : `Unresolved project/environment / ${app.name} (${app.uuid.slice(0, 8)})`;
    const option = new Option(label, app.uuid);
    option.disabled = !app.selectable;
    select.append(option);
  }
  if ([...select.options].some((item) => item.value === current)) select.value = current;
}

async function loadCoolify() {
  const status = await request('/api/coolify/status');
  coolifyAvailable = Boolean(status.configured);
  if (!coolifyAvailable) {
    byId('coolify-state').textContent = 'Coolify not connected';
    byId('coolify-hint').textContent = 'Set COOLIFY_API_URL and COOLIFY_API_TOKEN to connect Coolify.';
    byId('new-coolify-app').disabled = true;
    return;
  }
  try {
    coolifyApps = await request('/api/coolify/applications');
    fillChoices(byId('new-coolify-app'), true);
    const selectable = coolifyApps.filter((app) => app.selectable).length;
    byId('new-coolify-app').disabled = selectable === 0;
    byId('coolify-state').textContent = `${selectable} qualified Coolify apps available`;
    const unqualified = coolifyApps.length - selectable;
    byId('coolify-hint').textContent = unqualified
      ? `${unqualified} apps have no resolvable project/environment and cannot be selected. Check Coolify API permissions.`
      : 'Choose a full project / environment / app path. The UUID ensures the exact application is updated.';
  } catch (error) {
    byId('coolify-state').textContent = 'Coolify unavailable';
    byId('coolify-hint').textContent = `Coolify connection failed: ${error.message}`;
    byId('new-coolify-app').disabled = true;
    coolifyAvailable = false;
  }
}

async function linkToCoolify(name, uuid, envKey, replaceExisting, redeploy) {
  return request(`/api/apps/${encodeURIComponent(name)}/coolify`, {
    method: 'POST',
    body: JSON.stringify({
      coolify_app_uuid: uuid,
      env_key: envKey,
      replace_existing: replaceExisting,
      redeploy,
    }),
  });
}

async function loadApps() {
  const apps = await request('/api/apps');
  byId('app-count').textContent = String(apps.length).padStart(2, '0');
  byId('empty').hidden = apps.length !== 0;
  const list = byId('app-list');
  list.replaceChildren();
  for (const app of apps) {
    const row = elt('div', 'app-row');
    const icon = elt('div', 'db-icon', '▥');
    icon.setAttribute('aria-hidden', 'true');
    const info = elt('div', 'app-text');
    info.append(elt('div', 'app-title', app.name));
    info.append(elt('div', 'app-meta', `${app.database} · ${app.username}`));
    if (app.coolify_app_uuid) {
      info.append(elt('div', 'app-link', `↗ Coolify: ${coolifyName(app.coolify_app_uuid)} · ${app.coolify_env_key}`));
    } else {
      info.append(elt('div', 'app-link offline', '○ Standalone database'));
    }
    const actions = elt('div', 'app-actions');
    actions.append(button('Credentials', 'small-btn', async () => {
      showCredentials(await request(`/api/apps/${encodeURIComponent(app.name)}/credentials`), 'Current credentials');
    }));
    if (app.coolify_app_uuid) {
      actions.append(button('Sync + deploy', 'small-btn', async () => {
        if (!confirm(`Rewrite ${app.coolify_env_key} for ${coolifyName(app.coolify_app_uuid)} and queue a redeploy?`)) return;
        const result = await linkToCoolify(app.name, app.coolify_app_uuid, app.coolify_env_key, true, true);
        notify(result.warning || `Coolify ${result.env_action}; redeploy queued`, Boolean(result.warning));
      }));
      actions.append(button('Unlink', 'small-btn', async () => {
        if (!confirm(`Remove ${app.coolify_env_key} from ${coolifyName(app.coolify_app_uuid)} and unlink ${app.name}?\n\nThis does not delete the PostgreSQL database, but the Coolify app may lose DB access after its next deploy.`)) return;
        await request(`/api/apps/${encodeURIComponent(app.name)}/coolify/unlink`, {
          method: 'POST', body: JSON.stringify({remove_env: true, redeploy: false}),
        });
        notify('Coolify variable removed. The app was NOT redeployed.');
        await loadApps();
      }));
    } else if (coolifyAvailable && coolifyApps.some((resource) => resource.selectable)) {
      actions.append(button('Connect Coolify', 'small-btn', async () => openConnect(app.name)));
    }
    actions.append(button('Rotate', 'small-btn', async () => {
      const warning = app.coolify_app_uuid
        ? 'The manager will update Coolify and queue a redeploy. Existing DB connections may be briefly interrupted.'
        : 'You must update the DATABASE_URL of any app using this database.';
      if (!confirm(`Rotate ${app.name}'s database password?\n\n${warning}`)) return;
      const result = await request(`/api/apps/${encodeURIComponent(app.name)}/rotate`, {method: 'POST'});
      showCredentials(result, result.warning || (app.coolify_app_uuid
        ? 'New credentials saved in Coolify; application redeploy requested.'
        : 'New password. Update consuming applications manually.'));
      if (result.warning) notify(result.warning, true);
      else notify('Password rotated' + (result.redeploy_requested ? ', Coolify redeploy queued' : ''));
    }));
    actions.append(button('Delete', 'small-btn danger', async () => {
      if (app.coolify_app_uuid) {
        notify('Unlink Coolify before deleting this database', true);
        return;
      }
      const typed = prompt(`PERMANENTLY DROP database and role for ${app.name}, deleting ALL app data.\n\nType ${app.name} to confirm:`);
      if (typed !== app.name) {
        if (typed !== null) notify('Confirmation did not match', true);
        return;
      }
      await request(`/api/apps/${encodeURIComponent(app.name)}`, {
        method: 'DELETE', body: JSON.stringify({confirm: typed}),
      });
      notify(`${app.name} was deleted`);
      await loadApps();
    }));
    row.append(icon, info, actions);
    list.append(row);
  }
}

function showCredentials(credentials, description) {
  activeCredentials = credentials;
  byId('credentials-title').textContent = credentials.name;
  byId('credentials-description').textContent = description;
  const fields = byId('credential-fields');
  fields.replaceChildren();
  for (const [label, value] of [
    ['Database', credentials.database],
    ['Username', credentials.username],
    ['Password', credentials.password],
    ['DATABASE_URL', credentials.database_url],
  ]) {
    const container = elt('div', 'field');
    container.append(elt('label', '', label));
    const line = elt('div', 'field-value');
    const input = elt('input');
    input.readOnly = true;
    input.value = value;
    input.setAttribute('aria-label', label);
    input.addEventListener('focus', () => input.select());
    line.append(input);
    line.append(button('Copy', '', async () => {
      await navigator.clipboard.writeText(value);
      notify(`${label} copied`);
    }));
    container.append(line);
    fields.append(container);
  }
  credentialsDialog.showModal();
}

function closeCredentials() {
  credentialsDialog.close();
  activeCredentials = null;
  byId('credential-fields').replaceChildren();
}

function openConnect(name) {
  if (!coolifyAvailable || !coolifyApps.some((app) => app.selectable)) {
    notify('Connect to Coolify first', true);
    return;
  }
  editingApp = name;
  byId('connect-description').textContent = `Choose where to inject the credentials for ${name}.`;
  byId('connect-title').textContent = `Connect ${name}`;
  byId('connect-key').value = 'DATABASE_URL';
  byId('connect-replace').checked = false;
  byId('connect-redeploy').checked = true;
  fillChoices(byId('connect-app'));
  connectDialog.showModal();
}

byId('create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const name = byId('app-name').value.trim();
  const uuid = byId('new-coolify-app').value;
  const redeploy = byId('new-redeploy').checked;
  const replace = byId('new-replace').checked;
  const envKey = byId('new-env-key').value.trim();
  const btn = byId('create-button');
  btn.disabled = true;
  let credentials = null;
  try {
    credentials = await request('/api/apps', {method: 'POST', body: JSON.stringify({name})});
    byId('app-name').value = '';
    let description = 'New PostgreSQL database created. Store these credentials safely.';
    if (uuid) {
      try {
        const linked = await linkToCoolify(name, uuid, envKey, replace, redeploy);
        description = linked.warning || `Connected to ${coolifyName(uuid)}. ${redeploy ? 'Coolify deployment requested.' : 'Deploy the app when ready.'}`;
        if (linked.warning) notify(linked.warning, true);
      } catch (error) {
        description = `Database created, but Coolify linking failed: ${error.message}. Use Connect Coolify to retry.`;
        notify(description, true);
      }
    }
    showCredentials(credentials, description);
    await loadApps();
    if (!uuid) notify(`${name} was provisioned`);
  } catch (error) {
    notify(error.message, true);
  } finally {
    btn.disabled = false;
  }
});

byId('connect-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const btn = byId('connect-submit');
  btn.disabled = true;
  try {
    const result = await linkToCoolify(
      editingApp, byId('connect-app').value, byId('connect-key').value.trim(),
      byId('connect-replace').checked, byId('connect-redeploy').checked,
    );
    connectDialog.close();
    notify(result.warning || `Coolify variable ${result.env_action}; ${result.deployment_requested ? 'deployment queued' : 'deploy when ready'}`, Boolean(result.warning));
    await loadApps();
  } catch (error) {
    notify(error.message, true);
  } finally {
    btn.disabled = false;
  }
});

byId('refresh').addEventListener('click', async () => {
  try { await loadCoolify(); await loadApps(); notify('Apps refreshed'); }
  catch (error) { notify(error.message, true); }
});
byId('modal-close').addEventListener('click', closeCredentials);
byId('done').addEventListener('click', closeCredentials);
byId('copy-url').addEventListener('click', async () => {
  if (!activeCredentials) return;
  try { await navigator.clipboard.writeText(activeCredentials.database_url); notify('DATABASE_URL copied'); }
  catch (_) { notify('Copy failed; select the field and copy manually', true); }
});
credentialsDialog.addEventListener('close', () => {
  activeCredentials = null;
  byId('credential-fields').replaceChildren();
});
byId('connect-close').addEventListener('click', () => connectDialog.close());
byId('connect-cancel').addEventListener('click', () => connectDialog.close());
connectDialog.addEventListener('close', () => { editingApp = null; });
(async () => {
  await loadCoolify();
  await loadApps();
})().catch((error) => notify(error.message, true));
