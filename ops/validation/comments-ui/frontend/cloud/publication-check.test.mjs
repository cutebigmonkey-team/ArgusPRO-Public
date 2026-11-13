import test from 'node:test';
import assert from 'node:assert/strict';
import {publicationIdentity,checkPublication} from './publication-check.mjs';
test('canonical public URLs preserve identity and ignore known tracking',()=>{
  assert.equal(publicationIdentity('https://twitter.com/owner/status/123?utm_source=feed'),'x.com/owner/status/123');
  assert.equal(publicationIdentity('https://t.me/s/owner/42'),'t.me/owner/42');
  assert.equal(publicationIdentity('https://www.instagram.com/reel/ABC/?igsh=tracking'),'instagram.com/p/ABC');
  assert.equal(publicationIdentity('https://youtu.be/abcdefghijk?si=tracker'),'youtube.com/watch?v=abcdefghijk');
  assert.equal(publicationIdentity('https://facebook.com/permalink.php?story_fbid=123&id=42'),'facebook.com/permalink.php?id=42&story_fbid=123');
  assert.equal(publicationIdentity('javascript:alert(1)'),null);
});
test('archive checks distinguish DSNS from missing posts without inventing a cause',()=>{
  const post={url:'https://t.me/owner/42',platform:'telegram',source_id:'s',origin:'dsns'};
  assert.match(checkPublication([post],'https://t.me/s/owner/42',[],{}),/Є в архіві.*ДСНС/);
  const sources=[{id:'s',url:'https://t.me/owner'}];
  assert.match(checkPublication([],'https://t.me/owner/43',sources,{sources:{s:{status:'timeout'}}}),/timeout.*не підтверджена причина/);
  assert.match(checkPublication([],'https://t.me/someone/43',sources,{}),/не вдалося однозначно/);
});
