import {readKeywords,writeKeyword} from './drive.mjs';

const fold = text => text.normalize('NFC').toLowerCase();
function element(tag, text = '', className = '') {
  const el = document.createElement(tag);
  el.textContent = text;
  el.className = className;
  return el;
}
function button(text, action, style = 'secondary') {
  const el = element('button',text,'btn btn--sm btn--'+style);
  el.type = 'button';
  el.onclick = action;
  return el;
}

export function createKeywords() {
  let rows = [], selected = '', loaded = false, readError = '', saving = false, revision = 0;
  let updatePage, feedSelect;
  const selectedWord = () => rows.find(row => row.id === selected)?.word || '';
  function fillSelect(select) {
    select.replaceChildren(new Option('Усі пости без обмеження',''));
    for (const row of rows) select.add(new Option(row.word,row.id));
    select.value = selected;
    select.disabled = !rows.length;
    if (!rows.length) select.options[0].text = readError ? 'Не вдалося завантажити слова' :
      loaded ? 'Немає збережених слів' : 'Завантаження…';
  }
  function update(previousWord) {
    rows.sort((a,b) => a.word.localeCompare(b.word,'uk') || a.id.localeCompare(b.id));
    if (!selectedWord()) selected = '';
    updatePage?.();
    if (feedSelect?.isConnected) {
      fillSelect(feedSelect);
      if (previousWord !== selectedWord()) feedSelect.dispatchEvent(new Event('change'));
    }
  }
  async function refresh() {
    if (saving) return;
    const version = ++revision;
    try {
      const result = await readKeywords();
      if (version !== revision) return;
      const previous = selectedWord();
      rows = result;
      loaded = true;
      readError = '';
      update(previous);
    } catch (e) {
      if (version !== revision) return;
      readError = e.message;
      update(selectedWord());
    }
  }
  function mountSelect(select) {
    updatePage = null;
    feedSelect = select;
    select.onchange = () => { selected = select.value; };
    fillSelect(select);
  }
  function mount(main) {
    feedSelect = null;
    const title = element('h1','Ключові слова','page-header__title');
    const help = element('p','Збережіть слова або фрази, щоб знаходити їх у заголовках і текстах постів.','page-summary');
    const form = element('form','','keyword-tools');
    const input = element('input','','form-input');
    input.maxLength = 120;
    input.placeholder = 'Слово або фраза';
    input.setAttribute('aria-label',input.placeholder);
    const submit = element('button','Додати','btn btn--primary');
    submit.type = 'submit';
    let editing = null;
    function edit(row) {
      editing = row;
      input.value = row?.word || '';
      submit.textContent = row ? 'Зберегти' : 'Додати';
      cancel.hidden = !row;
      message.textContent = '';
      input.focus();
    }
    const cancel = button('Скасувати',() => edit(null));
    cancel.hidden = true;
    const message = element('p','','page-summary');
    message.setAttribute('role','status');
    const list = element('ul','','cloud-keywords');
    async function save(row, word) {
      if (saving || !loaded) return;
      if (word !== null && (!word || rows.some(r => r.id !== row?.id && fold(r.word) === fold(word)))) {
        message.textContent = word ? 'Таке ключове слово вже є.' : 'Введіть слово або фразу.';
        input.focus();
        return;
      }
      if (row && !rows.some(r => r.id === row.id && r.word === row.word)) {
        message.textContent = 'Слово вже змінено. Виберіть його зі списку ще раз.';
        return;
      }
      saving = true;
      ++revision;
      const previous = selectedWord();
      updatePage?.();
      try {
        const saved = await writeKeyword(row?.id,word);
        rows = rows.filter(r => r.id !== saved.id);
        if (word !== null) rows.push(saved);
        if (word !== null || editing?.id === saved.id) edit(null);
        message.textContent = word === null ? 'Слово видалено.' : 'Слово збережено.';
      } catch (e) {
        message.textContent = [403,404].includes(e.status) ? 'Для цієї зміни потрібне право редагувати файл у папці ARGUS.' : e.message;
      } finally {
        saving = false;
        update(previous);
      }
    }
    form.onsubmit = event => {
      event.preventDefault();
      save(editing,input.value.trim().normalize('NFC'));
    };
    form.append(input,submit,cancel);
    const notice = element('p','','page-summary');
    updatePage = () => {
      input.disabled = submit.disabled = cancel.disabled = saving || !loaded;
      notice.textContent = readError || (!loaded ? 'Завантаження…' : !rows.length ? 'Ключових слів ще немає.' : '');
      list.replaceChildren();
      for (const row of rows) {
        const item = element('li','','keyword-tools');
        const name = element('span',row.word);
        const show = element('a','У стрічку','btn btn--sm btn--secondary');
        show.href = '#/';
        show.onclick = () => { selected = row.id; };
        const rename = button('Редагувати',() => edit(row));
        const remove = button('Видалити',() => {
          if (window.confirm('Видалити «'+row.word+'»?')) save(row,null);
        },'ghost');
        rename.disabled = remove.disabled = saving;
        item.append(name,show,rename,remove);
        list.append(item);
      }
    };
    main.replaceChildren(title,help,form,message,notice,list);
    updatePage();
  }
  return {refresh,mount,mountSelect,selectedWord};
}
