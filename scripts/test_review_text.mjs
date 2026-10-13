import test from 'node:test';
import assert from 'node:assert/strict';
import {condition, describeBinding, feedbackPrompt} from '../sites/playground/review-text.mjs';

test('conditional summaries retain all branches and scope',()=>{
  const text=describeBinding({kind:'template',args:{template:'2022-10-29',where:{dim2:'Mexico3',week_ending:'2022-10-29'}}});
  assert.match(text,/港口分组是「Mexico3」/);assert.match(text,/Mexico 表格的日期或标签是「2022-10-29」/);
  assert.match(text,/填写「2022-10-29」/);
  const scope=describeBinding({kind:'row_scope',args:{mode:'any',conditions:[{column:'label',in:['A','B']},{column:'id',notnull:true}]}});
  assert.match(scope,/任一/);assert.match(scope,/A、B/);assert.match(scope,/id有值/);
  assert.equal(condition('label',{regex:'^(?:2021 YTD|Total 2021)'}),'label以「2021 YTD」或「Total 2021」开头');
  assert.equal(condition('label',{not_regex:'[pr]$'}),'label不以「p」或「r」结尾');
  assert.equal(condition('id',{notnull:false}),'id为空');
});

test('feedback is preserved as quoted data, and asks for the existing closed reply',()=>{
  const feedback='日期不对。\n"quoted" <script>not code</script>';
  const prompt=feedbackPrompt('closed instructions',feedback,{source_cell:'B6'});
  const data=JSON.parse(prompt.split('reply contract.\n')[1].split('\n\nReturn ONLY')[0]);
  assert.equal(data.feedback,feedback);assert.equal(data.selected_difference.source_cell,'B6');
  assert.match(prompt,/Include all conditional branches/);assert.match(prompt,/Do not return a full config/);
});
