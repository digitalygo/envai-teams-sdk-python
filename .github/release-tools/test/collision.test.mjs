import {execFile} from 'node:child_process';
import {mkdtemp,mkdir,rm,readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {promisify} from 'node:util';
const execute=promisify(execFile);
function fixtureEnv(home){return {PATH:process.env.PATH,HOME:home,LANG:'C.UTF-8',CI:'false',GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_ALLOW_PROTOCOL:'file',GIT_TERMINAL_PROMPT:'0',GIT_AUTHOR_NAME:'SDK Release Review',GIT_AUTHOR_EMAIL:'review@example.invalid',GIT_COMMITTER_NAME:'SDK Release Review',GIT_COMMITTER_EMAIL:'review@example.invalid'};}
async function createFixture({branch='main',occupiedTags=[],message='chore: test release',initialTag='v1.0.0'}={}){
 const root=await mkdtemp(join(tmpdir(),'sdk-review-release-'));const cwd=join(root,'work');const remote=join(root,'remote.git');await mkdir(cwd);const env=fixtureEnv(root);
 const git=async(...args)=>(await execute('git',args,{cwd,env})).stdout.trim();
 await git('init','--initial-branch=main');await git('init','--bare','--initial-branch=main',remote);await git('commit','--allow-empty','-m','feat: initial');if(initialTag)await git('tag',initialTag);
 for(const name of ['alpha','beta','dev'])await git('branch',name);
 if(occupiedTags.length){await git('checkout','--orphan','archived-release-line');await git('commit','--allow-empty','-m','chore: archived release');for(const tag of occupiedTags)await git('tag',tag);}
 await git('checkout',branch);await git('commit','--allow-empty','-m',message);await git('remote','add','origin',remote);await git('push','--all','origin');await git('push','--tags','origin');
 const refs=async()=>({local:await git('for-each-ref','--format=%(refname):%(objectname)','refs/tags','refs/notes'),remote:(await execute('git',['--git-dir',remote,'show-ref'],{cwd,env})).stdout});
 return {root,cwd,remote,env,git,refs,dispose:()=>rm(root,{recursive:true,force:true})};
}

const cases=[
 {name:'main baseline',expected:'1.0.1'},
 {name:'main orphan tag',occupiedTags:['v1.0.1'],expected:'1.0.2'},
 {name:'main three orphan tags',occupiedTags:['v1.0.1','v1.0.2','v1.0.3'],expected:'1.0.4'},
 {name:'alpha baseline',branch:'alpha',expected:'1.0.1-alpha.1'},
 {name:'alpha orphan tag',branch:'alpha',occupiedTags:['v1.0.1-alpha.1'],expected:'1.0.1-alpha.2'},
 {name:'beta orphan tags',branch:'beta',occupiedTags:['v1.0.1-beta.1','v1.0.1-beta.2'],expected:'1.0.1-beta.3'},
 {name:'dev collision',branch:'dev',occupiedTags:['v1.0.1-dev.1'],expected:'1.0.1-dev.2'},
 {name:'release record without tag',releaseTags:['v1.0.1'],expected:'1.0.2'},
 {name:'mixed tag and orphan release',occupiedTags:['v1.0.1','v1.0.3'],releaseTags:['v1.0.2'],expected:'1.0.4'},
 {name:'cross channel tag not collision',branch:'alpha',occupiedTags:['v1.0.1-beta.1'],expected:'1.0.1-alpha.1'},
 {name:'minor collision',message:'feat: extend',occupiedTags:['v1.1.0'],expected:'1.1.1'},
 {name:'major collision',message:'feat!: remove',occupiedTags:['v2.0.0'],expected:'2.0.1'},
 {name:'no-release docs',message:'docs: explain',occupiedTags:['v1.0.1'],expected:null},
 {name:'empty tag history',initialTag:null,expected:'1.0.0'},
];
for(const row of cases){test(`real release engine collision policy: ${row.name}`,{timeout:60000},async()=>{const f=await createFixture(row);try{const before=await f.refs();await execute(process.execPath,[fileURLToPath(new URL('semantic-release-dry-run.mjs',import.meta.url)),pathToFileURL(f.remote).href],{cwd:f.cwd,env:{...f.env,SDK_RELEASE_EXISTING_TAGS:JSON.stringify(row.releaseTags||[])},timeout:45000});const {result,publicationCalls,output}=JSON.parse(await readFile(f.cwd+'/dry-run-result.json','utf8'));assert.equal(result?.nextRelease?.version??null,row.expected);assert.deepEqual(publicationCalls,[]);assert.deepEqual(await f.refs(),before);if(row.expected)assert.match(output,/Skip .* tag creation in dry-run mode/);}finally{await f.dispose();}});}
