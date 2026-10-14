import { isHosted } from '@/lib/hosted';
import { Workspace } from '@/components/workspace';
export default async function Page({params}:{params:Promise<{id:string}>}){const {id}=await params;return <Workspace view="report" reportId={id} hostedMode={isHosted()}/>;}
