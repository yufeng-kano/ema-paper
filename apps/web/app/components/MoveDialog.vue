<script setup lang="ts">
/**
 * Where should this go? — the keyboard's route to a move (docs/web.md § Files), and the
 * destination of a parse filed from History (docs/web.md § History).
 *
 * Dragging a row onto a folder is the fast way and the one most people will use, but a drag
 * is invisible to a keyboard and to assistive tech, so every move is also available here.
 * It is the same tree, in a dialog, with a destination to pick.
 *
 * A folder cannot be moved into its own subtree; `forbidden` carries that set so the choice
 * is refused here rather than by the server after the fact.
 *
 * It can also make the folder it needs, inside the selected one. The folder is created
 * here, drawn from this dialog's own copy at once, and reported through `created` so the
 * caller's tree learns of it too (docs/web.md § Files). The own copy is what keeps it on
 * screen: a caller's poll already in flight can land after the create with a tree that
 * predates it.
 */
import { createFolder, type LibraryFolder } from '~/lib/api'
import { FOLDER_NAME_MAX, folderPath, type FolderId } from '~/lib/library'

const props = withDefaults(
  defineProps<{
    folders: LibraryFolder[]
    /** What is being placed — shown under the title. */
    name: string
    /** The verb, as the title and as the confirm button. Move, unless the caller says. */
    title?: string
    action?: string
    /**
     * Where it is now: preselected, and not a move if it is still chosen. Omitted for
     * something that is not in the library yet — then every folder is a destination.
     */
    current?: FolderId
    /** Preselected when there is no `current`. */
    start?: FolderId
    /** Destinations this item may not enter. */
    forbidden?: Set<number>
    pending?: boolean
    /** Why the last attempt failed — inside the dialog, which is where it happened. */
    error?: string | null
  }>(),
  {
    title: undefined,
    action: undefined,
    current: undefined,
    start: null,
    forbidden: () => new Set<number>(),
    error: null,
  },
)

const emit = defineEmits<{ choose: [FolderId]; created: [LibraryFolder]; cancel: [] }>()

const { t } = useI18n()
const { resolve } = useApiError()

/** Folders made here, until the caller's own tree carries them. */
const made = ref<LibraryFolder[]>([])
const tree = computed(() => [
  ...props.folders,
  ...made.value.filter((folder) => !props.folders.some((known) => known.id === folder.id)),
])

const destination = ref<FolderId>(props.current === undefined ? props.start : props.current)
/** Opened down to the preselected folder, so the dialog starts on the part of the tree that
 *  is about it rather than on a collapsed root. */
const expanded = ref(new Set(folderPath(tree.value, destination.value).map((row) => row.id)))

function toggle(id: number) {
  if (expanded.value.has(id)) {
    expanded.value.delete(id)
  } else {
    expanded.value.add(id)
  }
}

const heading = computed(() => props.title ?? t('files.moveTitle'))

// --- a new folder, inside the selected one ---------------------------------

const naming = ref(false)
const newName = ref('')
const creating = ref(false)
const createError = ref<string | null>(null)
const nameField = ref<HTMLElement | null>(null)

const destinationName = computed(
  () => tree.value.find((folder) => folder.id === destination.value)?.name ?? t('files.root'),
)
/** No folder inside a place the item could not go anyway. */
const canCreateHere = computed(
  () => destination.value === null || !props.forbidden.has(destination.value),
)

/** Prefilled and selected, as a file manager's new folder is: Enter accepts the default. */
async function startNaming() {
  naming.value = true
  newName.value = t('files.newFolderName')
  createError.value = null
  await nextTick()
  nameField.value?.querySelector('input')?.select()
}

/** Escape closes the field, not the dialog — `UiModal` listens on the document, after this. */
function stopNaming(event?: KeyboardEvent) {
  event?.stopPropagation()
  if (!creating.value) {
    naming.value = false
    createError.value = null
  }
}

function choose() {
  createError.value = null
  emit('choose', destination.value)
}

async function createHere() {
  const name = newName.value.trim()
  // Re-checked here: the tree stays clickable while the field is open. And never beside a
  // move or add in flight — that one closes the dialog, taking this one's outcome with it.
  if (!name || busy.value || !canCreateHere.value) {
    return
  }
  creating.value = true
  createError.value = null
  try {
    const folder = await createFolder({ name, parent_id: destination.value })
    made.value.push(folder)
    emit('created', folder)
    if (destination.value !== null) {
      expanded.value.add(destination.value)
    }
    destination.value = folder.id
    naming.value = false
  } catch (error) {
    createError.value = await resolve(error)
  } finally {
    creating.value = false
  }
}

const busy = computed(() => props.pending || creating.value)

const canChoose = computed(
  () =>
    destination.value !== props.current &&
    !(destination.value !== null && props.forbidden.has(destination.value)),
)
</script>

<template>
  <UiModal :title="heading" :busy="busy" @close="emit('cancel')">
    <template #title>
      <h2 class="title">{{ heading }}</h2>
      <p class="subject">{{ name }}</p>
    </template>

    <UiBanner v-if="createError ?? error" class="failure" tone="error">
      {{ createError ?? error }}
    </UiBanner>

    <div class="picker">
      <FolderTree
        :folders="tree"
        :selected="destination"
        :expanded="expanded"
        @select="destination = $event"
        @toggle="toggle"
      />
    </div>

    <form
      v-if="naming"
      ref="nameField"
      class="new-folder"
      @submit.prevent="createHere"
      @keydown.esc="stopNaming"
    >
      <UiField v-slot="{ id }" :label="t('files.newFolderIn', { folder: destinationName })">
        <div class="new-folder-row">
          <UiTextInput
            :id="id"
            v-model="newName"
            :maxlength="FOLDER_NAME_MAX"
            :disabled="busy"
            required
          />
          <UiButton
            type="submit"
            :disabled="!newName.trim() || !canCreateHere || pending"
            :loading="creating"
          >
            {{ t('files.create') }}
          </UiButton>
          <UiButton
            variant="ghost"
            icon-only
            :label="t('common.cancel')"
            :disabled="busy"
            @click="stopNaming()"
          >
            <template #icon><UiIcon name="close" /></template>
          </UiButton>
        </div>
      </UiField>
    </form>

    <template #footer>
      <!-- Left, away from the confirm button: making a folder is not finishing the dialog. -->
      <UiButton
        class="new-folder-button"
        variant="ghost"
        :disabled="busy || naming || !canCreateHere"
        @click="startNaming"
      >
        <template #icon><UiIcon name="folder-plus" /></template>
        {{ t('files.newFolder') }}
      </UiButton>
      <UiButton variant="ghost" :disabled="busy" @click="emit('cancel')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton
        variant="primary"
        :disabled="!canChoose || creating"
        :loading="pending"
        @click="choose"
      >
        {{ action ?? t('files.move') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.title {
  font-size: var(--display-sm);
  letter-spacing: normal;
}

/* The thing being moved, under the verb — a fact, not a sentence explaining the dialog. */
.subject {
  margin-top: var(--space-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--faint);
  font-size: var(--text-xs);
}

.failure {
  margin-bottom: var(--space-4);
}

.new-folder {
  margin-top: var(--space-4);
}

.new-folder-row {
  display: flex;
  gap: var(--space-2);
}

.new-folder-row > :first-child {
  flex: 1;
  min-width: 0;
}

.new-folder-button {
  margin-right: auto;
}

/* Bounded and scrolling: a deep tree must not push the dialog's buttons off the screen. */
.picker {
  max-height: 320px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-2);
  border: 1px solid var(--line-strong);
  background: var(--rail);
}
</style>
