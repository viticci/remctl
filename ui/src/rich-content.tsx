import React, {useEffect, useRef, useState} from 'react';
import {AppWindow, ArrowUpRight, Globe} from 'lucide-react';
import {app, isAppLink, linkScheme, RecordData as D, safeLink} from './bridge';
import {notePresentation} from './reminder-helpers';

// A small shared cache avoids downloading the same Apple preview after every refresh.
const previews = new Map<string, Promise<D>>();
const queue: (() => void)[] = [];
let active = 0;
function loadPreview(uri: string, revision: string) {
  const key = uri + ':' + revision;
  if (!previews.has(key)) {
    const promise = new Promise<D>((resolve, reject) => {
      queue.push(() => {
        active++;
        app.readServerResource({uri}).then(result => {
          const content = result.contents.find(c => 'text' in c);
          if (!content || !('text' in content)) throw new Error('No saved preview');
          return JSON.parse(content.text);
        }).then(resolve, reject).finally(() => {active--; pump();});
      });
      pump();
    });
    previews.set(key, promise);
    promise.catch(() => previews.delete(key));
    if (previews.size > 12) previews.delete(previews.keys().next().value!);
  }
  return previews.get(key)!;
}
function pump() {while (active < 3 && queue.length) queue.shift()!();}

export function useNearViewport() {
  const ref = useRef<HTMLDivElement>(null), [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {setVisible(true); observer.disconnect();}
    }, {rootMargin: '240px'});
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return {ref, visible};
}

function LinkedText({text, run}: {text:string;run:(fn:()=>Promise<any>)=>any}) {
  return <>{text.split(/(https?:\/\/[^\s<>]+)/g).map((part, index) => /^https?:\/\//.test(part)
    ? <a key={index} href={part} onClick={e => {e.preventDefault();e.stopPropagation();run(() => safeLink(part));}}>{part}</a>
    : part)}</>;
}

export function NotePreview({item, run}: {item:D;run:(fn:()=>Promise<any>)=>any}) {
  const [expanded, setExpanded] = useState(false);
  const note = notePresentation(item.notes);
  if (!note.text) return null;
  const long = note.text.length > 240 || note.text.split('\n').length > 3;
  return <div className={'task-note' + (expanded ? ' expanded' : '')}>
    {note.label && <div className="note-label">{note.label}{note.version && <span>{note.version}</span>}</div>}
    <div className="note-text"><LinkedText text={note.text} run={run}/></div>
    {long && <button className="note-expand" aria-expanded={expanded} aria-label={`${expanded ? 'Collapse' : 'Expand'} notes: ${item.title}`} onClick={e => {e.stopPropagation();setExpanded(!expanded);}}>{expanded ? 'Less' : 'More'}</button>}
  </div>;
}

export function SavedLinkCard({link, title, run}: {link:D;title:string;run:(fn:()=>Promise<any>)=>any}) {
  const {ref, visible} = useNearViewport();
  const [preview, setPreview] = useState<D|null>(null);
  useEffect(() => {
    let live = true;
    setPreview(null);
    if (visible && link.resourceUri) loadPreview(link.resourceUri, link.revision || '').then(value => {if(live)setPreview(value);}).catch(() => {});
    return () => {live=false;};
  }, [visible, link.resourceUri, link.revision]);
  const appLink = isAppLink(link.url);
  let domain = appLink ? linkScheme(link.url) : '';
  if (!appLink) try {domain=new URL(link.url).hostname.replace(/^www\./,'');}catch{}
  if (!appLink && !/^https?:\/\//i.test(link.url || '')) return null;
  const image = preview?.image, icon = preview?.icon;
  const src = (asset:D) => /^image\/(png|jpeg|gif|webp)$/.test(asset.mimeType) ? `data:${asset.mimeType};base64,${asset.data}` : undefined;
  return <div ref={ref} className="saved-link-wrap">
    <a className={'saved-link-card' + (image ? ' has-artwork' : '')} href={link.url} aria-label={`Open ${preview?.title || title || domain}`} onClick={e=>{e.preventDefault();e.stopPropagation();run(()=>safeLink(link.url));}}>
      {image && <div className="saved-link-artwork"><img src={src(image)} alt="" loading="lazy" onLoad={e=>{const image=e.currentTarget;image.classList.toggle("square-artwork",image.naturalWidth/image.naturalHeight >= .85 && image.naturalWidth/image.naturalHeight < 1.3);}}/></div>}
      <div className="saved-link-caption">
        {icon ? <img className="saved-link-icon" src={src(icon)} alt=""/> : !image && (appLink ? <AppWindow size={23} className="saved-link-globe"/> : <Globe size={23} className="saved-link-globe"/>)}
        <div><strong>{preview?.title || title || domain}</strong><span>{preview?.siteName || domain}</span></div><ArrowUpRight size={14}/>
      </div>
    </a>
  </div>;
}

export function RichLinks({item, run}: {item:D;run:(fn:()=>Promise<any>)=>any}) {
  const links:D[] = item.links?.length ? item.links : item.url ? [{url:item.url}] : [];
  return <>{links.map((link, index) => <SavedLinkCard key={link.resourceUri || link.url + index} link={link} title={item.title} run={run}/>)}</>;
}
