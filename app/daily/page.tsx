import { isHosted } from '@/lib/hosted';
import { Workspace } from '@/components/workspace';
export const dynamic='force-dynamic';
export default function Page(){return <Workspace view="daily" hostedMode={isHosted()}/>;}
